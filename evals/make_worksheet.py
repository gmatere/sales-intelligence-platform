#!/usr/bin/env python3
"""Render a batch of unlabelled entities as a plain-text worksheet.

Hand-editing JSONL is slow and easy to corrupt, and the labels are the only
artefact in this repo that cannot be regenerated. So labelling happens in a
flat text file where the only thing to type is one word per entity, and
`merge_labels.py` puts it back.

Usage:
    python make_worksheet.py [batch.jsonl] [labels.txt]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

HEADER = """# LABELLING WORKSHEET
#
# After each "->" type ONE of these six words:
#
#   company      a normal business (shop, factory, law firm, SaaS, charity)
#   hosting      sells web hosting, servers, VPS, cloud
#   isp          internet or phone provider
#   security     CDN / WAF / security vendor (a competitor)
#   government   council, ministry, university, school, public body
#   unknown      you genuinely cannot tell
#
# Anything after a "#" is a note to yourself — worth writing on the ones you
# found hard. When a label and the model disagree later, that note is how you
# tell which of you was wrong.
#
# Use `unknown` freely. A set with no ambiguous cases measures the easy half of
# the job and flatters the score.
#
# Under each line: country, hosts, ports, the registered org name (usually the
# hosting provider, NOT the company), and detected software.
# ----------------------------------------------------------------------

"""


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "evals" / "batch.jsonl"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else REPO / "evals" / "labels.txt"

    if not src.exists():
        print(f"no batch at {src} — run build_labelled_set.py first")
        sys.exit(1)

    rows = [json.loads(line) for line in src.open(encoding="utf-8")]
    lines = [HEADER]

    for i, r in enumerate(rows, 1):
        products = ", ".join(r.get("products") or [])[:60] or "none"
        org = (r.get("orgs_seen") or "none")[:70]
        lines.append(f"{i:>2}. {r['entity_domain']:<34} -> \n")
        lines.append(f"      {r.get('primary_country') or '??'} · "
                     f"{r['n_hosts']} hosts · {r['n_ports']} ports · org: {org}\n")
        lines.append(f"      running: {products}\n\n")

    out.write_text("".join(lines), encoding="utf-8")
    print(f"wrote {len(rows)} entities to {out}")
    print(f"open with:  open -e {out}")


if __name__ == "__main__":
    main()
