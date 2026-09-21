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


def already_in_set() -> set[str]:
    """Entities already present in the labelled set.

    Excluded from new batches so the set can be grown incrementally without
    re-labelling work already done, and without a duplicate quietly
    double-weighting one entity in the score.
    """
    if not OUT.exists():
        return set()
    seen = set()
    for line in OUT.open(encoding="utf-8"):
        try:
            seen.add(json.loads(line)["entity_domain"])
        except (json.JSONDecodeError, KeyError):
            continue
    return seen


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


def draw(conn, where: str, limit: int) -> list[dict]:
    """Random rows matching a predicate.

    `ORDER BY random() LIMIT n` rather than `USING SAMPLE`: sampling clauses
    interact badly with narrow predicates over a join and returned far fewer
    rows than asked for, silently.
    """
    cols = ", ".join(f"q.{c}" for c in EVIDENCE)
    return conn.sql(f"""
        SELECT {cols}, c.sequential_name_ratio, c.rule_evidence
        FROM llm_classification_queue q
        JOIN int_entity_classification c USING (entity_domain)
        WHERE {where}
        ORDER BY random()
        LIMIT {limit}
    """).df().to_dict("records")


def sample(conn, exclude: set[str], n: int) -> list[dict]:
    """Stratified across estate size, weighted toward the boundary cases.

    Bands are a target, not a guarantee — after infrastructure exclusion some
    are nearly empty. Shortfalls are topped up from the general pool and the
    per-band counts are printed, because an eval set that quietly came back a
    third of the requested size is worse than one that failed.
    """
    blocked = ", ".join(f"'{d}'" for d in exclude) or "''"
    not_excluded = f"q.entity_domain NOT IN ({blocked})"
    per_band = max(1, n // 5)

    bands = [
        ("single host", f"q.n_hosts = 1 AND {not_excluded}"),
        ("2-10 hosts", f"q.n_hosts BETWEEN 2 AND 10 AND {not_excluded}"),
        ("11-100 hosts", f"q.n_hosts BETWEEN 11 AND 100 AND {not_excluded}"),
        ("over 100 hosts", f"q.n_hosts > 100 AND {not_excluded}"),
        # Entities the hosting heuristic almost excluded. Over-represented on
        # purpose: the boundary decides the precision number.
        ("near-miss on hosting heuristic",
         f"c.sequential_name_ratio BETWEEN 0.4 AND 0.79 AND {not_excluded}"),
    ]

    seen, picked = set(), []
    for label, predicate in bands:
        got = 0
        for row in draw(conn, predicate, per_band):
            if row["entity_domain"] in seen:
                continue
            seen.add(row["entity_domain"])
            picked.append(row)
            got += 1
        print(f"  {label:<34} {got:>2} of {per_band}")

    if len(picked) < n:
        shortfall = n - len(picked)
        blocked_now = ", ".join(f"'{d}'" for d in seen | exclude) or "''"
        for row in draw(conn, f"q.entity_domain NOT IN ({blocked_now})", shortfall):
            seen.add(row["entity_domain"])
            picked.append(row)
        print(f"  {'topped up from general pool':<34} {len(picked) - (n - shortfall):>2}")

    return picked[:n]


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
    batch = Path(sys.argv[2]) if len(sys.argv) > 2 else REPO / "evals" / "batch.jsonl"

    # New entities go to a separate batch file rather than into the labelled set
    # directly. Writing straight into the set risks clobbering hand-applied
    # labels, and those are the only artefact here that cannot be regenerated.
    existing = already_in_set()
    examples = example_entities()
    exclude = existing | examples

    conn = duckdb.connect(WAREHOUSE, read_only=True)
    print(f"sampling {n} new entities "
          f"({len(existing)} already labelled, {len(examples)} prompt examples):")
    rows = sample(conn, exclude, n)
    conn.close()

    if len(rows) < n:
        print(f"\nonly {len(rows)} available — the queue may be smaller than "
              f"expected, or exclusions too broad")

    batch.parent.mkdir(parents=True, exist_ok=True)
    with batch.open("w", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(to_record(row), ensure_ascii=False) + "\n")

    print(f"wrote {len(rows)} entities to {batch}")
    print(f"excluded {len(examples)} prompt worked-examples: {sorted(examples)}\n")
    print("Fill `true_class` on every line with one of:")
    for cls in CLASSES:
        print(f"  {cls}")
    print("\nUse `unknown` where the evidence genuinely does not settle it — an")
    print("eval set with no ambiguous cases measures the easy half of the job.")
    print("Put your reasoning in `labeller_note`; it is what you will read when")
    print("a disagreement turns out to be the label's fault rather than the model's.")


if __name__ == "__main__":
    main()
