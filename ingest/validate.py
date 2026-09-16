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
    ("records with product", "SELECT count(*) FROM src WHERE product IS NOT NULL"),
    ("expired certs", "SELECT count(*) FROM src WHERE ssl_expired"),
    ("missing security.txt", "SELECT count(*) FROM src WHERE NOT has_securitytxt"),
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
