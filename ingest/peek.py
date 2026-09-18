#!/usr/bin/env python3
"""Inspect raw fields in the source dump without re-ingesting.

Answers "is there a field that would help with X, and did we project it?"
by sampling the head of the compressed source and reporting every JSON path
with its fill rate. Optionally prints sample values for one path.

Usage:
    python peek.py <source.json.zst>                    # inventory, 20k sample
    python peek.py <source.json.zst> 50000              # larger sample
    python peek.py <source.json.zst> 20000 http.waf     # values for one path
"""

from __future__ import annotations

import io
import sys
from collections import Counter
from pathlib import Path

import orjson
import zstandard

DEFAULT_SAMPLE = 20_000
MAX_DEPTH = 3


def walk(node: object, prefix: str = "") -> list[str]:
    """Dotted paths for every populated field, capped at MAX_DEPTH."""
    if prefix.count(".") >= MAX_DEPTH:
        return [prefix]

    if isinstance(node, dict):
        if not node:
            return []
        paths = []
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else key
            paths.extend(walk(value, child))
        return paths

    if isinstance(node, list):
        return [prefix] if node else []

    return [prefix] if node is not None else []


def sample_records(source: Path, limit: int):
    """Yield up to `limit` decoded records from the head of the dump."""
    handle = open(source, "rb")
    decompressor = zstandard.ZstdDecompressor(max_window_size=2 ** 31)
    stream = io.TextIOWrapper(
        io.BufferedReader(decompressor.stream_reader(handle), buffer_size=1 << 22),
        encoding="utf-8",
        errors="replace",
    )

    for index, line in enumerate(stream):
        if index >= limit:
            return
        try:
            yield orjson.loads(line)
        except orjson.JSONDecodeError:
            continue


def dig(record: dict, path: str):
    """Follow a dotted path, returning None if any hop is missing."""
    node = record
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node


def report_inventory(counts: Counter, total: int) -> None:
    print(f"\n{len(counts)} distinct paths across {total:,} sampled records\n")
    print(f"  {'path':<42} {'filled':>10}   {'rate':>7}")
    print(f"  {'-' * 42} {'-' * 10}   {'-' * 7}")
    for path, count in counts.most_common():
        print(f"  {path:<42} {count:>10,}   {100.0 * count / total:>6.2f}%")


def report_values(values: Counter, path: str) -> None:
    print(f"\ntop values for '{path}' ({sum(values.values()):,} populated)\n")
    for value, count in values.most_common(25):
        text = str(value)
        display = text if len(text) <= 70 else text[:67] + "..."
        print(f"  {count:>8,}  {display}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    source = Path(sys.argv[1])
    if not source.exists():
        print(f"source not found: {source}")
        sys.exit(1)

    limit = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SAMPLE
    target = sys.argv[3] if len(sys.argv) > 3 else None

    counts: Counter = Counter()
    values: Counter = Counter()
    total = 0

    for record in sample_records(source, limit):
        total += 1
        counts.update(walk(record))
        if not target:
            continue
        found = dig(record, target)
        if found is not None:
            values[orjson.dumps(found).decode() if isinstance(found, (dict, list)) else found] += 1

    if not total:
        print("no records read")
        sys.exit(1)

    report_inventory(counts, total)
    if target:
        report_values(values, target)


if __name__ == "__main__":
    main()
