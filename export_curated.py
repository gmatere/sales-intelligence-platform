#!/usr/bin/env python3
"""Build the small Parquet the app serves.

The boundary between pipeline and product. Everything upstream runs on a
machine that holds 12 GB of scan data; everything downstream runs from this
file, which is a few thousand rows and a few megabytes.

Three things happen here and nowhere else:

Classification verdicts are merged back in, so tiers can finally be assigned.
Until the LLM has spoken, every unresolved entity sits in `U - unclassified`
and no account can be a prospect.

Raw IP addresses are dropped. A rep needs the company, the finding and the
reason to call — never an address. Excluding them also means what lands on a
laptop is ordinary business data rather than a slice of an internet scan.

The result is a serving artifact, not a database. No credentials, no runtime
inference, nothing for the app to get wrong.

Usage:
    python export_curated.py [out.parquet]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent
WAREHOUSE = "/root/warehouse.duckdb"
CLASSIFICATIONS = REPO / "data" / "entity_classifications.jsonl"
DEFAULT_OUT = REPO / "app" / "data" / "accounts.parquet"

# Only entities the model confirmed as real organisations become prospects.
# Government and education are kept: they are genuine buyers, flagged so the
# app can separate them, because the sales motion differs.
PROSPECT_CLASSES = ("end_customer_company", "government_or_education")

# Below this the model was, by its own account, guessing. A low-confidence
# "company" is exactly the input that puts a datacenter in a call list.
MIN_CONFIDENCE = 0.60


def load_classifications(conn, version: str, model: str) -> int:
    """Load verdicts from exactly one (prompt version, model) configuration.

    Filtering matters more than it looks. The results file accumulates every
    run ever made, and two runs of the same prompt on different models both
    carry the same `prompt_version` — so ordering by version alone breaks ties
    arbitrarily and ships a silent blend of two models. The shipped artifact
    should be attributable to one configuration, which is also the only one the
    eval measured.
    """
    if not CLASSIFICATIONS.exists():
        print(f"no classifications at {CLASSIFICATIONS}")
        sys.exit(1)

    seen, kept = 0, {}
    for line in CLASSIFICATIONS.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        seen += 1
        if r.get("prompt_version") != version or r.get("model") != model:
            continue
        # Later lines win: the file is append-only, so file order is run order.
        kept[r["entity_domain"]] = r

    if not kept:
        models = sorted({(json.loads(l).get("prompt_version"),
                          json.loads(l).get("model"))
                         for l in CLASSIFICATIONS.open(encoding="utf-8")})
        print(f"no rows for {version} / {model}. Available configurations:")
        for v, m in models:
            print(f"  {v} / {m}")
        sys.exit(1)

    conn.sql("DROP TABLE IF EXISTS classifications")
    conn.sql("""
        CREATE TABLE classifications (
            entity_domain VARCHAR, prompt_version VARCHAR, model VARCHAR,
            entity_class VARCHAR, canonical_name VARCHAR,
            confidence DOUBLE, reasoning VARCHAR
        )
    """)
    conn.executemany(
        "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(r["entity_domain"], r["prompt_version"], r.get("model", ""),
          r["entity_class"], r["canonical_name"], r["confidence"],
          r["reasoning"]) for r in kept.values()],
    )
    print(f"{seen:,} rows in file, {len(kept):,} for {version} / {model}")
    return len(kept)


def build(conn, out: Path) -> None:
    prospects = ", ".join(f"'{c}'" for c in PROSPECT_CLASSES)

    conn.sql(f"""
        CREATE OR REPLACE TABLE curated AS
        SELECT
            s.entity_domain,
            coalesce(c.canonical_name, s.entity_domain)        AS company,
            c.entity_class,
            c.confidence                                       AS class_confidence,
            c.reasoning                                        AS class_reasoning,

            -- Tier is reassigned here because it depends on the verdict that
            -- only exists after enrichment. The dbt model can only ever say
            -- 'U - unclassified' for these.
            CASE
                WHEN c.entity_class IS NULL                       THEN 'U - unclassified'
                WHEN c.entity_class NOT IN ({prospects})          THEN 'X - not a prospect'
                WHEN c.confidence < {MIN_CONFIDENCE}              THEN 'R - needs review'
                WHEN s.fit_score >= 50 AND s.intent_score >= 50   THEN 'A - call now'
                WHEN s.fit_score >= 50                            THEN 'B - nurture'
                WHEN s.intent_score >= 50                         THEN 'C - opportunistic'
                ELSE 'D - deprioritise'
            END                                                AS tier,

            s.fit_score, s.intent_score,
            s.primary_country, s.primary_country_name, s.primary_region, s.primary_city,
            s.n_hosts, s.n_ports, s.n_products, s.n_countries,

            s.max_epss, s.max_cvss, s.n_cves_critical, s.n_cves_high, s.n_cve_findings,
            s.headline_cve, s.headline_cve_epss, s.headline_cve_cvss,
            s.headline_cve_summary, s.headline_cve_product, s.headline_cve_version,

            s.n_eol_services, s.n_expired_certs, s.soonest_cert_expiry_days,
            s.n_deprecated_tls, s.n_dead_ssl, s.n_self_signed, s.n_weak_cert_sig,
            s.n_exposed_datastores, s.n_remote_access, s.n_exposed_cameras,
            s.n_open_directories, s.n_iot_devices, s.heartbleed_vulnerable,

            s.no_waf_anywhere, s.waf_vendors, s.publishes_securitytxt,
            s.technologies, s.products, s.n_signal_categories,

            -- Scoring components, retained so the app can show why an account
            -- ranked where it did. "The model said so" loses the call.
            s.pts_epss, s.pts_critical_cve, s.pts_datastore, s.pts_remote_access,
            s.pts_eol, s.pts_expired_cert, s.pts_open_dir, s.pts_cameras,
            s.pts_dead_ssl, s.pts_cert_expiring, s.pts_deprecated_tls,
            s.pts_size_band, s.pts_whitespace, s.pts_real_estate,
            s.pts_multi_country, s.pts_low_maturity,

            s.last_seen_at

        FROM company_scores s
        LEFT JOIN classifications c USING (entity_domain)
        WHERE c.entity_class IS NOT NULL
    """)

    out.parent.mkdir(parents=True, exist_ok=True)
    conn.sql(f"COPY curated TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-version", default="v3")
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    out = args.out
    conn = duckdb.connect(WAREHOUSE, read_only=False)

    n = load_classifications(conn, args.prompt_version, args.model)
    print(f"{n:,} classified entities from {args.prompt_version} / {args.model}")

    build(conn, out)

    print(f"\n{'tier':<22}{'n':>7}")
    for tier, count in conn.sql(
        "SELECT tier, count(*) FROM curated GROUP BY 1 ORDER BY 1"
    ).fetchall():
        print(f"{tier:<22}{count:>7,}")

    size_mb = out.stat().st_size / 1e6
    print(f"\nwrote {out}  ({size_mb:.1f} MB)")
    if size_mb > 40:
        print("Larger than a repo should carry — tighten the WHERE clause "
              "before committing it for deployment.")
    conn.close()


if __name__ == "__main__":
    main()
