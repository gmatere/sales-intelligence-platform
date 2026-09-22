#!/usr/bin/env python3
"""Measure the exact cacheable prefix of a prompt version.

The cache threshold applies to the prefix before the breakpoint — tools plus
system — and nothing else. Every previous attempt to check that against the
floor used a chars-per-token estimate or a subtraction from observed totals,
and both were wrong by enough to matter against a hard cutoff.

`count_tokens` reports it exactly and costs nothing. Use this rather than
arithmetic before claiming a prompt will or will not cache.

Usage:
    python measure_prefix.py [prompt_version] [model_override]
"""

from __future__ import annotations

import sys

import anthropic

from classify import TOOL, load_prompt, render
from tracing import cache_floor

# A deliberately tiny user message: we want the prefix, and anything in the
# message inflates the total without contributing to the cached block.
MINIMAL = {k: "x" for k in (
    "entity_domain", "orgs_seen", "primary_country", "n_hosts", "n_ports",
    "n_products", "products", "technologies", "waf_vendors", "cloud_host_ratio")}


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else "v3"
    model, system, template = load_prompt(version)
    if len(sys.argv) > 2:
        model = sys.argv[2]

    client = anthropic.Anthropic()
    user = render(template, MINIMAL)

    # Two counts: with the prefix, and with a stub system block. The difference
    # isolates tools + system from the message and the request scaffolding.
    full = client.messages.count_tokens(
        model=model,
        system=[{"type": "text", "text": system}],
        tools=[TOOL],
        messages=[{"role": "user", "content": user}],
    ).input_tokens

    bare = client.messages.count_tokens(
        model=model,
        system=[{"type": "text", "text": "x"}],
        messages=[{"role": "user", "content": user}],
    ).input_tokens

    prefix = full - bare
    floor = cache_floor(model)
    margin = prefix - floor

    print(f"prompt version   : {version}")
    print(f"model            : {model}")
    print(f"total w/ message : {full:,}")
    print(f"message + frame  : {bare:,}")
    print(f"cacheable prefix : {prefix:,}")
    print(f"floor            : {floor:,}")
    print(f"margin           : {margin:+,}")
    print()
    if margin >= 0:
        print("Above the floor — this prompt should cache. Confirm against "
              "cache_creation_input_tokens on a real call; a count is a "
              "prediction, an observed write is proof.")
    else:
        print(f"Below the floor by {abs(margin):,} tokens — cache_control will "
              f"be ignored silently, with no error and no warning. Add at least "
              f"{abs(margin):,} tokens to the system block to clear it.")


if __name__ == "__main__":
    main()
