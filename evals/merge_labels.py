#!/usr/bin/env python3
"""Merge the plain-text worksheet back into the labelled set.

Editing JSONL by hand is error-prone and slow, so labelling happens in
`labels.txt` — one short word per line — and this merges it into
`labelled_set.jsonl`, expanding the shorthand to the full class names the
harness expects.

Refuses to write anything if a label is unrecognised or an entity is missing,
rather than silently dropping rows: a labelled set that quietly lost a third of
its entries produces a confident, wrong number.

Usage:
    python merge_labels.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKSHEET = REPO / "evals" / "labels.txt"
LABELLED = REPO / "evals" / "labelled_set.jsonl"

SHORTHAND = {
    "company": "end_customer_company",
    "hosting": "hosting_or_cloud",
    "isp": "isp_telco",
    "security": "cdn_or_security_vendor",
    "government": "government_or_education",
    "unknown": "unknown",
}


def parse_worksheet() -> dict[str, tuple[str, str]]:
    """Domain -> (class, note) from lines shaped `12. domain.tld -> word # note`."""
    parsed, problems = {}, []

    for raw in WORKSHEET.read_text(encoding="utf-8").splitlines():
        if raw.lstrip().startswith("#") or "->" not in raw:
            continue

        left, _, right = raw.partition("->")
        domain_match = re.search(r"\d+\.\s*(\S+)", left)
        if not domain_match:
            continue
        domain = domain_match.group(1)

        answer, _, note = right.partition("#")
        answer = answer.strip().lower()

        if not answer:
            problems.append(f"  {domain}: no label")
            continue
        if answer not in SHORTHAND:
            problems.append(f"  {domain}: {answer!r} is not one of "
                            f"{', '.join(SHORTHAND)}")
            continue

        parsed[domain] = (SHORTHAND[answer], note.strip())

    if problems:
        print(f"{len(problems)} problem(s) — nothing written:\n")
        print("\n".join(problems))
        sys.exit(1)

    return parsed


def main() -> None:
    if not WORKSHEET.exists():
        print(f"no worksheet at {WORKSHEET}")
        sys.exit(1)

    labels = parse_worksheet()

    # Labels may belong to a fresh batch or to the existing set. Read both,
    # apply labels wherever they match, and append anything new — so the set
    # can be grown in passes without ever rewriting labels already applied.
    batch = REPO / "evals" / "batch.jsonl"
    existing = ([json.loads(l) for l in LABELLED.open(encoding="utf-8")]
                if LABELLED.exists() else [])
    incoming = ([json.loads(l) for l in batch.open(encoding="utf-8")]
                if batch.exists() else [])

    known = {r["entity_domain"] for r in existing}
    rows = existing + [r for r in incoming if r["entity_domain"] not in known]

    missing = [r["entity_domain"] for r in rows
               if r["entity_domain"] not in labels and not r.get("true_class")]
    if missing:
        print(f"{len(missing)} entities have no label — nothing written:\n")
        for domain in missing:
            print(f"  {domain}")
        sys.exit(1)

    for row in rows:
        if row["entity_domain"] in labels:
            row["true_class"], note = labels[row["entity_domain"]]
            if note:
                row["labeller_note"] = note

    with LABELLED.open("w", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["true_class"]] = counts.get(row["true_class"], 0) + 1

    print(f"merged {len(rows)} labels into {LABELLED.name}\n")
    for cls, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {cls:<26} {n:>3}")

    if len(counts) < 3:
        print("\nOnly a couple of distinct classes. Per-class precision needs "
              "examples of each — consider whether the sample is representative.")


if __name__ == "__main__":
    main()
