#!/usr/bin/env python3
"""Stream a Shodan .json.zst dump into narrow Parquet shards.

Projects ~30 columns out of ~8.8M records (~74GB decompressed) without ever
materialising the decompressed JSON on disk. Resumable: re-running skips
records already covered by completed shards.

Usage:
    python ingest.py <source.json.zst> <out_dir>
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import tldextract
import zstandard

SHARD_SIZE = 100_000
READ_BUFFER = 1 << 22

# Shodan nests each detected service under its own top-level key. Their
# presence is the exposure signal, so we record which keys appeared rather
# than carrying the (large, heterogeneous) payloads through.
SERVICE_KEYS = (
    "redis", "mysql", "mysqlx", "mongodb", "elastic", "postgres",
    "ssh", "ftp", "telnet", "rdp_encryption", "snmp", "ntp", "dns",
    "mdns", "kubernetes", "docker", "vnc", "smb", "ldap",
    "hikvision", "dahua_dvr_web", "plex", "pptp", "ntlm", "screenshot",
)

SCHEMA = pa.schema([
    ("ip", pa.string()),
    ("port", pa.int32()),
    ("transport", pa.string()),
    ("scanned_at", pa.string()),
    ("asn", pa.string()),
    ("org", pa.string()),
    ("isp", pa.string()),
    ("hostnames", pa.list_(pa.string())),
    ("domains", pa.list_(pa.string())),
    ("primary_domain", pa.string()),
    ("country_code", pa.string()),
    ("country_name", pa.string()),
    ("region_code", pa.string()),
    ("city", pa.string()),
    ("product", pa.string()),
    ("version", pa.string()),
    ("os", pa.string()),
    ("devicetype", pa.string()),
    ("info", pa.string()),
    ("cpe23", pa.list_(pa.string())),
    ("tags", pa.list_(pa.string())),
    ("cves", pa.list_(pa.string())),
    ("n_cves", pa.int32()),
    ("cloud_provider", pa.string()),
    ("cloud_service", pa.string()),
    ("scan_module", pa.string()),
    ("http_status", pa.int32()),
    ("http_title", pa.string()),
    ("http_server", pa.string()),
    ("http_waf", pa.string()),
    ("http_components", pa.list_(pa.string())),
    ("has_securitytxt", pa.bool_()),
    ("ssl_versions", pa.list_(pa.string())),
    ("ssl_issuer", pa.string()),
    ("ssl_cert_cn", pa.string()),
    ("ssl_sig_alg", pa.string()),
    ("ssl_issued", pa.string()),
    ("ssl_expires", pa.string()),
    ("ssl_expired", pa.bool_()),
    ("heartbleed", pa.string()),
    ("services", pa.list_(pa.string())),
])

# cache_dir keeps the public suffix list local so repeat runs are offline
# and deterministic.
_extract = tldextract.TLDExtract(cache_dir="/tmp/tldextract-cache")


def primary_domain(hostnames: list[str]) -> str | None:
    """Shortest registrable domain across hostnames, as the entity anchor.

    Shortest wins because `api.acme.com` and `acme.com` should both resolve
    to `acme.com` rather than to whichever hostname happened to come first.
    """
    if not hostnames:
        return None

    candidates = set()
    for hostname in hostnames:
        parts = _extract(hostname)
        if parts.domain and parts.suffix:
            candidates.add(f"{parts.domain}.{parts.suffix}")

    if not candidates:
        return None
    return min(candidates, key=len)


def present_services(record: dict) -> list[str]:
    return [key for key in SERVICE_KEYS if key in record]


def heartbleed_verdict(opts: dict) -> str | None:
    """Trailing verdict token from Shodan's heartbleed probe.

    Raw form is '2026/09/14 09:59:55 23.4.47.75:443 - SAFE'. Only the verdict
    carries meaning; the timestamp and address are already columns. Unlike
    `vulns`, this is a tested result rather than a version inference.
    """
    raw = (opts.get("heartbleed") or "").strip()
    if not raw:
        return None
    return raw.rsplit(" - ", 1)[-1].strip() or None


def project(record: dict) -> dict:
    """Flatten one Shodan record down to the analysis columns."""
    location = record.get("location") or {}
    http = record.get("http") or {}
    ssl = record.get("ssl") or {}
    cert = ssl.get("cert") or {}
    cloud = record.get("cloud") or {}
    hostnames = record.get("hostnames") or []
    cves = sorted((record.get("vulns") or {}).keys())
    components = record.get("http", {}).get("components") or {}

    return {
        "ip": record.get("ip_str"),
        "port": record.get("port"),
        "transport": record.get("transport"),
        "scanned_at": record.get("timestamp"),
        "asn": record.get("asn"),
        "org": record.get("org"),
        "isp": record.get("isp"),
        "hostnames": hostnames,
        "domains": record.get("domains") or [],
        "primary_domain": primary_domain(hostnames),
        "country_code": location.get("country_code"),
        "country_name": location.get("country_name"),
        "region_code": location.get("region_code"),
        "city": location.get("city"),
        "product": record.get("product"),
        "version": record.get("version"),
        "os": record.get("os"),
        "devicetype": record.get("devicetype"),
        "info": record.get("info"),
        "cpe23": record.get("cpe23") or [],
        "tags": record.get("tags") or [],
        "cves": cves,
        "n_cves": len(cves),
        "cloud_provider": cloud.get("provider"),
        "cloud_service": cloud.get("service"),
        "scan_module": (record.get("_shodan") or {}).get("module"),
        "http_status": http.get("status"),
        "http_title": http.get("title"),
        "http_server": http.get("server"),
        "http_waf": http.get("waf"),
        "http_components": sorted(components.keys()),
        "has_securitytxt": bool(http.get("securitytxt")),
        "ssl_versions": ssl.get("versions") or [],
        "ssl_issuer": (cert.get("issuer") or {}).get("O"),
        "ssl_cert_cn": (cert.get("subject") or {}).get("CN"),
        "ssl_sig_alg": cert.get("sig_alg"),
        "ssl_issued": cert.get("issued"),
        "ssl_expires": cert.get("expires"),
        "ssl_expired": bool(cert.get("expired")),
        "heartbleed": heartbleed_verdict(record.get("opts") or {}),
        "services": present_services(record),
    }


class ShardWriter:
    """Buffers projected rows and flushes fixed-size Parquet shards."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.rows: list[dict] = []
        self.shard_index = len(list(self.out_dir.glob("part-*.parquet")))

    @property
    def records_already_done(self) -> int:
        return self.shard_index * SHARD_SIZE

    def add(self, row: dict) -> None:
        self.rows.append(row)
        if len(self.rows) >= SHARD_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return

        # Write under a temp name the resume glob ignores, then rename. Parquet
        # writes its footer last, so a crash mid-write would otherwise leave a
        # truncated file that resume counts as complete — silently skipping
        # SHARD_SIZE records. Rename is atomic within a filesystem.
        final = self.out_dir / f"part-{self.shard_index:05d}.parquet"
        staging = self.out_dir / f".part-{self.shard_index:05d}.inflight"

        table = pa.Table.from_pylist(self.rows, schema=SCHEMA)
        pq.write_table(table, staging, compression="zstd")
        staging.replace(final)

        self.rows = []
        self.shard_index += 1


def open_stream(source: Path):
    """Line iterator over the decompressed dump.

    max_window_size is raised because long-range-matched zstd frames fail to
    open with the default window.
    """
    handle = open(source, "rb")
    decompressor = zstandard.ZstdDecompressor(max_window_size=2 ** 31)
    reader = decompressor.stream_reader(handle)
    return io.TextIOWrapper(
        io.BufferedReader(reader, buffer_size=READ_BUFFER),
        encoding="utf-8",
        errors="replace",
    )


def run(source: Path, out_dir: Path) -> None:
    writer = ShardWriter(out_dir)
    skip = writer.records_already_done
    if skip:
        print(f"resuming: skipping {skip:,} records already sharded")

    seen = 0
    malformed = 0
    started = time.time()

    for line in open_stream(source):
        seen += 1
        if seen <= skip:
            continue

        try:
            record = orjson.loads(line)
        except orjson.JSONDecodeError:
            malformed += 1
            continue

        writer.add(project(record))

        if seen % 500_000 == 0:
            rate = seen / (time.time() - started)
            print(f"{seen:,} records | {rate:,.0f}/s | {malformed} malformed")

    writer.flush()
    elapsed = time.time() - started
    print(f"done: {seen:,} records in {elapsed/60:.1f} min, {malformed} malformed")
    print(f"shards written to {out_dir}")


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)

    source = Path(sys.argv[1])
    if not source.exists():
        print(f"source not found: {source}")
        sys.exit(1)

    run(source, Path(sys.argv[2]))


if __name__ == "__main__":
    main()
