#!/usr/bin/env python3
"""Diagnose why prompt caching is or is not engaging.

Three identical sequential calls against the real prompt, printing the complete
usage object each time. Sequential rather than concurrent so that calls two and
three unambiguously follow a completed write — concurrency makes a cold first
wave look identical to a cache that never works.

Expected when caching works:
    call 1  cache_creation_input_tokens > 0, input_tokens small
    call 2  cache_read_input_tokens > 0,     input_tokens small
    call 3  same as call 2

Usage:
    python check_cache.py [prompt_version] [model_override]
"""

from __future__ import annotations

import json
import sys

import anthropic

from classify import TOOL, load_prompt, render

SAMPLE = {
    "entity_domain": "example-diagnostic.com",
    "orgs_seen": "example org",
    "primary_country": "GB",
    "n_hosts": 7,
    "n_ports": 4,
    "n_products": 3,
    "products": ["nginx", "OpenSSH", "Postfix"],
    "technologies": ["Nginx", "Ubuntu"],
    "waf_vendors": [],
    "cloud_host_ratio": 0.4,
}


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else "v3"
    model, system, template = load_prompt(version)
    if len(sys.argv) > 2:
        model = sys.argv[2]
    prompt = render(template, SAMPLE)
    client = anthropic.Anthropic()

    print(f"prompt version : {version}")
    print(f"model          : {model}")
    print(f"system chars   : {len(system):,}")
    print(f"tool chars     : {len(json.dumps(TOOL)):,}")
    print(f"user chars     : {len(prompt):,}\n")

    for attempt in (1, 2, 3):
        response = client.messages.create(
            model=model,
            max_tokens=400,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "record_classification"},
            messages=[{"role": "user", "content": prompt}],
        )
        usage = response.usage.model_dump()
        print(f"call {attempt}: {json.dumps(usage, default=str)}")

    print("\nRead: a non-zero cache_creation_input_tokens on call 1 means the "
          "block was accepted for caching. A non-zero cache_read_input_tokens "
          "on calls 2-3 means it is being reused. Zero on both, with "
          "input_tokens carrying the full prompt, means the block was rejected "
          "outright — check the minimum for this model and that nothing in the "
          "prefix varies between calls.")


if __name__ == "__main__":
    main()
