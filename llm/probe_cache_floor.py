#!/usr/bin/env python3
"""Find the real minimum cacheable prefix for a model, empirically.

The documented minimum for Haiku 4.5 is 4096 tokens, but a measured ~4,283-token
block was still refused. Rather than argue with the documentation, sweep block
sizes and observe where caching actually begins.

Two calls per size: the first should write, the second should read. Reporting
both distinguishes "never accepted" from "accepted but not reused".

Filler is deterministic and prose-like — repeated random tokens would compress
differently and repeated identical sentences risk being handled specially.

Usage:
    python probe_cache_floor.py [model] [--tool] [--no-force]

    --tool      include the classifier tool schema (matches production shape)
    --no-force  use tool_choice auto instead of forcing the tool
"""

from __future__ import annotations

import json
import sys

import anthropic

from classify import TOOL

SIZES = [3500, 4000, 4096, 4300, 4600, 5000, 6000, 8000, 12000]

# ~10 tokens per line, prose-like, deterministic.
LINE = ("Operational guidance for classifying internet-exposed estates by "
        "ownership category and commercial relevance. ")


def system_of(target_tokens: int) -> str:
    """Approximate a target token count with repeated prose."""
    approx_tokens_per_line = len(LINE) / 3.5
    repeats = int(target_tokens / approx_tokens_per_line) + 1
    return "".join(f"{i}. {LINE}\n" for i in range(repeats))


def probe(client, model: str, size: int, use_tool: bool, force: bool) -> dict:
    system = system_of(size)
    kwargs = {
        "model": model,
        "max_tokens": 16,
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
    }
    if use_tool:
        kwargs["tools"] = [TOOL]
        if force:
            kwargs["tool_choice"] = {"type": "tool", "name": "record_classification"}

    usages = []
    for _ in range(2):
        response = client.messages.create(**kwargs)
        usages.append(response.usage.model_dump())
    return {"target": size, "first": usages[0], "second": usages[1]}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    model = args[0] if args else "claude-haiku-4-5"
    use_tool = "--tool" in sys.argv
    force = "--no-force" not in sys.argv

    client = anthropic.Anthropic()
    print(f"model   : {model}")
    print(f"tools   : {'included' if use_tool else 'omitted'}"
          f"{' (forced)' if use_tool and force else ''}\n")
    print(f"{'target':>7} {'total':>9} {'prefix':>9} {'write':>8} {'read':>8}  verdict")

    # Track the cacheable prefix, not total input. The threshold applies to the
    # prefix up to the breakpoint; the user message sits after it and is never
    # part of the cached block. Reporting totals overstates the prefix by
    # however long the message happens to be — which is precisely the error
    # that made a block ~34 tokens short of the floor look like it cleared it.
    largest_refused = 0
    smallest_cached = None

    for size in SIZES:
        try:
            r = probe(client, model, size, use_tool, force)
        except anthropic.APIError as exc:
            print(f"{size:>7} {'-':>9} {'-':>9} {'-':>8} {'-':>8}  ERROR {exc}")
            continue

        write = r["first"].get("cache_creation_input_tokens", 0) or 0
        read = r["second"].get("cache_read_input_tokens", 0) or 0
        fresh = r["first"].get("input_tokens", 0) or 0
        prior = r["first"].get("cache_read_input_tokens", 0) or 0

        # The prefix is whichever of write/read is non-zero. On a cold run the
        # first call writes it; on a warm run (a previous sweep left the same
        # prefix cached) the first call already reads it, and `fresh` is only
        # the user message.
        prefix = write or prior or read
        total = fresh + write + prior
        cached = bool(write or read)

        if cached and smallest_cached is None:
            smallest_cached = prefix
        elif not cached:
            largest_refused = max(largest_refused, total)

        print(f"{size:>7} {total:>9,} {prefix:>9,} {write:>8,} {read:>8,}  "
              f"{'CACHED' if cached else 'refused'}")

    print()
    if smallest_cached:
        print(f"Smallest prefix that cached : {smallest_cached:,} tokens")
        if largest_refused:
            print(f"Largest total that refused  : {largest_refused:,} tokens "
                  f"(prefix was smaller — the user message is excluded)")
        print(f"\nThe floor applies to the cacheable prefix, not to total input. "
              f"Measure the prefix from `cache_creation_input_tokens` on a cold "
              f"call; estimating it from character counts is how a block lands "
              f"just under the threshold and looks like it cleared.")
    else:
        print(f"No size cached for {model} at any tested prefix. That points "
              f"away from a size threshold — suspect the request shape or an "
              f"account-level setting rather than the prompt.")


if __name__ == "__main__":
    main()
