#!/usr/bin/env python3
"""Build a hand-labelling worksheet from the classification queue.

Emits one JSONL line per entity with all the evidence the classifier sees and
an empty `true_class` for a human to fill. Nothing is pre-filled and no label
is suggested: a suggested label is a label you will agree with, which makes the
resulting measurement worthless.

Two properties matter.

Stratified, not random. A random sample of the queue is dominated by whatever
is most common, and the classifier's accuracy on the common case is not the
thing in doubt. The sample deliberately spans estate sizes and includes the
entities the rules very nearly excluded — the boundary cases are where the
number is actually decided.

Leakage-proof. Entities named as worked examples in any prompt version are
excluded automatically by reading the prompt files, not by remembering to.

Usage:
    python build_labelled_set.py [n]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent.parent
WAREHOUSE = "/root/warehouse.duckdb"
OUT = REPO / "evals" / "labelled_set.jsonl"

CLASSES = [
    "end_customer_company",
    "hosting_or_cloud",
    "cdn_or_security_vendor",
    "isp_telco",
    "government_or_education",
    "unknown",
]

# Evidence fields shown to the labeller — exactly what the classifier receives,
# so the human and the model are judging the same thing.
EVIDENCE = [
    "entity_domain", "orgs_seen", "primary_country", "n_hosts", "n_ports",
    "n_products", "products", "technologies", "waf_vendors", "cloud_host_ratio",
]


def example_entities() -> set[str]:
    """Domains appearing as worked examples in any prompt version.

    Parsed from the prompt files rather than maintained as a list here, so a
    new example added to a prompt cannot silently leak into the eval set.
    """
    found = set()
    for path in (REPO / "prompts").glob("*/entity_classification.md"):
        text = path.read_text(encoding="utf-8")
        # Worked examples are introduced as **`domain.tld`** at line start.
        for match in re.finditer(r"^\*\*`([a-z0-9][a-z0-9.-]*\.[a-z]{2,})`\*\*",
                                 text, re.M | re.I):
            found.add(match.group(1).lower())
    return found


def sample(conn, exclude: set[str], n: int) -> list[dict]:
    """Stratified across estate size, weighted toward the boundary cases."""
    blocked = ", ".join(f"'{d}'" for d in exclude) or "''"
    cols = ", ".join(f"q.{c}" for c in EVIDENCE)

    # Bands chosen so each contains a genuinely different kind of entity:
    # single-host stubs, small businesses, the mid-market target, and estates
    # large enough to be either an enterprise or a reseller.
    # Qualified with q. — n_hosts exists in both joined relations.
    bands = [("q.n_hosts = 1", n // 5), ("q.n_hosts BETWEEN 2 AND 10", n // 5),
             ("q.n_hosts BETWEEN 11 AND 100", n // 5), ("q.n_hosts > 100", n // 5)]

    rows = []
    for predicate, count in bands:
        rows += conn.sql(f"""
            SELECT {cols}, c.sequential_name_ratio, c.rule_evidence
            FROM llm_classification_queue q
            JOIN int_entity_classification c USING (entity_domain)
            WHERE {predicate} AND q.entity_domain NOT IN ({blocked})
            USING SAMPLE {count} ROWS
        """).df().to_dict("records")

    # Near-misses: entities the hosting heuristic almost excluded. These decide
    # the precision number, so they are over-represented on purpose.
    rows += conn.sql(f"""
        SELECT {cols}, c.sequential_name_ratio, c.rule_evidence
        FROM llm_classification_queue q
        JOIN int_entity_classification c USING (entity_domain)
        WHERE c.sequential_name_ratio BETWEEN 0.4 AND 0.79
          AND q.entity_domain NOT IN ({blocked})
        USING SAMPLE {n - 4 * (n // 5)} ROWS
    """).df().to_dict("records")

    seen, unique = set(), []
    for row in rows:
        if row["entity_domain"] in seen:
            continue
        seen.add(row["entity_domain"])
        unique.append(row)
    return unique


def to_record(row: dict) -> dict:
    """One worksheet line: evidence, a blank label, and a note field."""
    def clean(value):
        if hasattr(value, "tolist"):
            value = value.tolist()
        if isinstance(value, list):
            return [str(v) for v in value if v is not None][:12]
        return value.item() if hasattr(value, "item") else value

    return {
        **{field: clean(row.get(field)) for field in EVIDENCE},
        "sequential_name_ratio": round(float(row.get("sequential_name_ratio") or 0), 2),
        "true_class": "",
        "labeller_note": "",
    }


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25

    if OUT.exists():
        print(f"{OUT} already exists — refusing to overwrite hand-applied labels.")
        print("Delete it explicitly if you intend to rebuild the worksheet.")
        sys.exit(1)

    exclude = example_entities()
    conn = duckdb.connect(WAREHOUSE, read_only=True)
    rows = sample(conn, exclude, n)
    conn.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(to_record(row), ensure_ascii=False) + "\n")

    print(f"wrote {len(rows)} entities to {OUT}")
    print(f"excluded {len(exclude)} prompt worked-examples: {sorted(exclude)}\n")
    print("Fill `true_class` on every line with one of:")
    for cls in CLASSES:
        print(f"  {cls}")
    print("\nUse `unknown` where the evidence genuinely does not settle it — an")
    print("eval set with no ambiguous cases measures the easy half of the job.")
    print("Put your reasoning in `labeller_note`; it is what you will read when")
    print("a disagreement turns out to be the label's fault rather than the model's.")


if __name__ == "__main__":
    main()
