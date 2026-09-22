#!/usr/bin/env python3
"""Report whether a prompt's cacheable prefix clears the model's floor.

Makes one real call and reads the answer, rather than computing it.

An earlier version of this script estimated the prefix by subtracting a
token count of the message from a count of the whole request. That
over-reported by ~315 tokens — enough to declare a prompt 86 tokens *above* a
floor it was actually ~230 tokens below. Three separate attempts to reason
about this threshold went wrong the same way: every one computed a number
instead of observing one.

`cache_creation_input_tokens` on a cold call is the cacheable block, exactly,
as the API accounts for it. Nothing else is authoritative. A call costs a
fraction of a cent, which is less than the cost of being wrong about it again.

Usage:
    python measure_prefix.py [prompt_version] [model_override]
"""

from __future__ import annotations

import sys

import anthropic

from classify import TOOL, load_prompt, render
from tracing import cache_floor

# Minimal message: we want the prefix, and message content sits after the
# breakpoint where it contributes nothing to the cached block.
MINIMAL = {k: "x" for k in (
    "entity_domain", "orgs_seen", "primary_country", "n_hosts", "n_ports",
    "n_products", "products", "technologies", "waf_vendors", "cloud_host_ratio")}


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else "v3"
    model, system, template = load_prompt(version)
    if len(sys.argv) > 2:
        model = sys.argv[2]

    client = anthropic.Anthropic()
    floor = cache_floor(model)

    # max_tokens is 1 because the response is irrelevant — we only want the
    # usage accounting, and generation is the expensive half.
    usage = client.messages.create(
        model=model,
        max_tokens=1,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL],
        messages=[{"role": "user", "content": render(template, MINIMAL)}],
    ).usage.model_dump()

    written = usage.get("cache_creation_input_tokens", 0) or 0
    read = usage.get("cache_read_input_tokens", 0) or 0
    fresh = usage.get("input_tokens", 0) or 0
    prefix = written or read

    print(f"prompt version   : {version}")
    print(f"model            : {model}")
    print(f"floor            : {floor:,}")
    print(f"cacheable prefix : {prefix:,}" if prefix else
          f"cacheable prefix : none — block rejected")
    print(f"uncached input   : {fresh:,}")
    print()

    if prefix:
        print(f"CACHES. Prefix clears the floor by {prefix - floor:+,} tokens, "
              f"observed rather than estimated.")
        if prefix - floor < 200:
            print(f"Margin is thin. Tokenisation varies between models and "
                  f"shifts with any prompt edit, so a prefix this close to the "
                  f"floor can silently drop under it.")
    else:
        print(f"DOES NOT CACHE. The whole prompt was billed as fresh input "
              f"({fresh:,} tokens), so `cache_control` was ignored.")
        print(f"The block is somewhere below {floor:,}; the API does not report "
              f"the size of a block it rejected. Add content and re-run until "
              f"this reports a write — bisecting with probe_cache_floor.py is "
              f"faster than guessing how much to add.")


if __name__ == "__main__":
    main()
