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
    print(f"{'target':>7} {'actual in':>10} {'write':>8} {'read':>8}  verdict")

    first_cached = None
    for size in SIZES:
        try:
            r = probe(client, model, size, use_tool, force)
        except anthropic.APIError as exc:
            print(f"{size:>7} {'-':>10} {'-':>8} {'-':>8}  ERROR {exc}")
            continue

        actual = (r["first"].get("input_tokens", 0)
                  + (r["first"].get("cache_creation_input_tokens", 0) or 0))
        write = r["first"].get("cache_creation_input_tokens", 0) or 0
        read = r["second"].get("cache_read_input_tokens", 0) or 0
        ok = write > 0 or read > 0
        if ok and first_cached is None:
            first_cached = actual
        print(f"{size:>7} {actual:>10,} {write:>8,} {read:>8,}  "
              f"{'CACHED' if ok else 'refused'}")

    print()
    if first_cached:
        print(f"Caching begins at roughly {first_cached:,} actual input tokens "
              f"for {model}.")
    else:
        print(f"No size cached for {model}. That points away from a size "
              f"threshold — suspect the request shape or an account-level "
              f"setting rather than the prompt.")


if __name__ == "__main__":
    main()
