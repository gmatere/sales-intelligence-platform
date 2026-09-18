#!/usr/bin/env python3
"""Profile the ingested Parquet shards.

Reports the numbers that drive downstream design decisions: entity coverage,
the distinct-domain count that sets the LLM cost ceiling, and signal density
for the scoring model.

Usage:
    python validate.py <parquet_dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

CHECKS = (
    ("total records", "SELECT count(*) FROM src"),
    ("distinct IPs", "SELECT count(DISTINCT ip) FROM src"),
    (
        "primary_domain coverage",
        """SELECT round(100.0 * count(primary_domain) / count(*), 2) || '%'
           FROM src""",
    ),
    (
        "distinct primary_domain  <-- LLM cost driver",
        "SELECT count(DISTINCT primary_domain) FROM src",
    ),
    ("distinct org", "SELECT count(DISTINCT org) FROM src"),
    ("records with CVEs", "SELECT count(*) FROM src WHERE n_cves > 0"),
    ("distinct CVEs", "SELECT count(DISTINCT c) FROM (SELECT unnest(cves) AS c FROM src)"),
    ("records with a critical CVE (cvss>=9)", "SELECT count(*) FROM src WHERE n_cves_critical > 0"),
    ("records with a VERIFIED CVE", "SELECT count(*) FROM src WHERE n_cves_verified > 0"),
    (
        "records with EPSS > 0.1  <-- actively exploited",
        "SELECT count(*) FROM src WHERE max_epss > 0.1",
    ),
    (
        "domains with EPSS > 0.1  <-- hottest prospects",
        "SELECT count(DISTINCT primary_domain) FROM src WHERE max_epss > 0.1",
    ),
    ("records with product", "SELECT count(*) FROM src WHERE product IS NOT NULL"),
    ("expired certs", "SELECT count(*) FROM src WHERE ssl_expired"),
    ("HAS security.txt  <-- maturity marker", "SELECT count(*) FROM src WHERE has_securitytxt"),
    ("records with a WAF", "SELECT count(*) FROM src WHERE http_waf IS NOT NULL"),
    (
        "domains with NO waf anywhere  <-- whitespace",
        """SELECT count(*) FROM (
             SELECT primary_domain FROM src WHERE primary_domain IS NOT NULL
             GROUP BY 1 HAVING count(http_waf) = 0)""",
    ),
    ("heartbleed probed", "SELECT count(*) FROM src WHERE heartbleed IS NOT NULL"),
    ("deprecated SHA-1 certs", "SELECT count(*) FROM src WHERE ssl_sig_alg ILIKE '%sha1%'"),
    (
        "extra domains from cert CN  <-- coverage lift",
        """SELECT count(DISTINCT ssl_cert_cn) FROM src
           WHERE primary_domain IS NULL AND ssl_cert_cn IS NOT NULL""",
    ),
)

BREAKDOWNS = (
    (
        "top 15 tags",
        """SELECT t AS tag, count(*) AS n
           FROM (SELECT unnest(tags) AS t FROM src)
           GROUP BY t ORDER BY n DESC LIMIT 15""",
    ),
    (
        "top 12 countries",
        """SELECT country_name, count(*) AS n FROM src
           WHERE country_name IS NOT NULL
           GROUP BY country_name ORDER BY n DESC LIMIT 12""",
    ),
    (
        "top 15 org  <-- expect infrastructure providers, not prospects",
        """SELECT org, count(*) AS n FROM src
           WHERE org IS NOT NULL
           GROUP BY org ORDER BY n DESC LIMIT 15""",
    ),
    (
        "top 12 exposed services",
        """SELECT s AS service, count(*) AS n
           FROM (SELECT unnest(services) AS s FROM src)
           GROUP BY s ORDER BY n DESC LIMIT 12""",
    ),
    (
        "top 10 ports",
        """SELECT port, count(*) AS n FROM src
           GROUP BY port ORDER BY n DESC LIMIT 10""",
    ),
    (
        "top 15 WAF vendors  <-- incumbent competitor signal",
        """SELECT http_waf, count(*) AS n FROM src
           WHERE http_waf IS NOT NULL
           GROUP BY 1 ORDER BY n DESC LIMIT 15""",
    ),
    (
        "top 15 detected technologies",
        """SELECT c AS technology, count(*) AS n
           FROM (SELECT unnest(http_components) AS c FROM src)
           GROUP BY c ORDER BY n DESC LIMIT 15""",
    ),
    (
        "scan modules flagging malware/RAT  <-- exclude these",
        """SELECT scan_module, count(*) AS n FROM src
           WHERE scan_module ILIKE '%rat%' OR scan_module ILIKE '%malware%'
              OR scan_module ILIKE '%c2%' OR scan_module ILIKE '%botnet%'
           GROUP BY 1 ORDER BY n DESC LIMIT 15""",
    ),
)


def run_scalars(conn) -> None:
    for label, sql in CHECKS:
        value = conn.sql(sql).fetchone()[0]
        printable = f"{value:,}" if isinstance(value, int) else value
        print(f"  {label:<45} {printable}")


def run_breakdowns(conn) -> None:
    for label, sql in BREAKDOWNS:
        print(f"\n--- {label} ---")
        conn.sql(sql).show(max_rows=20)


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)

    parquet_dir = Path(sys.argv[1])
    shards = sorted(parquet_dir.glob("part-*.parquet"))
    if not shards:
        print(f"no shards found in {parquet_dir}")
        sys.exit(1)

    print(f"{len(shards)} shards in {parquet_dir}\n")

    conn = duckdb.connect()
    conn.sql(f"CREATE VIEW src AS SELECT * FROM read_parquet('{parquet_dir}/part-*.parquet')")

    run_scalars(conn)
    run_breakdowns(conn)


if __name__ == "__main__":
    main()
