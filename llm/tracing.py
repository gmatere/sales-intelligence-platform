#!/usr/bin/env python3
"""Append-only JSONL trace of every model call.

One line per call, written immediately rather than buffered, so a run killed
halfway still leaves a complete record of what it spent and decided. The schema
is the point: prompt version and model are recorded per call, which is what
makes a v1-vs-v2 comparison possible after the fact rather than only while the
harness is running.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# USD per million tokens. Keyed by family prefix rather than exact model ID:
# an exact-match table returns 0.0 for an unrecognised ID, so renaming the model
# in a prompt file would silently zero out every cost figure in the traces.
PRICING = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cached_input": 0.10},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cached_input": 0.20},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cached_input": 0.50},
}

# Minimum cacheable prefix, per model. Below this, `cache_control` is ignored
# with no error and no warning — the only evidence is cache tokens staying at
# zero. Deliberately non-monotonic across generations: Haiku 4.5 requires 8x
# what Opus 5 does.
MIN_CACHEABLE_TOKENS = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-opus-4-7": 2048,
    "claude-haiku-4-5": 4096,
    "claude-opus-4-6": 4096,
}


def rates_for(model: str) -> dict | None:
    for prefix, rates in PRICING.items():
        if model.startswith(prefix):
            return rates
    return None


def cache_floor(model: str) -> int:
    for prefix, floor in MIN_CACHEABLE_TOKENS.items():
        if model.startswith(prefix):
            return floor
    return 4096  # assume the strictest known floor rather than under-report


@dataclass
class TraceRecord:
    """One model call. Field names are the log schema — changing them breaks
    every eval comparison already on disk."""

    task: str
    prompt_version: str
    model: str
    subject: str
    decision: str | None
    confidence: float | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    # Cache writes and cache reads are different events with different prices.
    # Recording only reads makes "the cache never engaged" indistinguishable
    # from "written every call, never read" — which are different bugs.
    cache_write_tokens: int
    cost_usd: float
    latency_ms: int
    attempt: int
    error: str | None = None
    request: dict = field(default_factory=dict)
    response: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


# Writing to the cache costs more than a plain input token; reading from it
# costs far less. Omitting the write multiplier understates every estimate.
CACHE_WRITE_MULTIPLIER = 1.25


def price_call(usage: dict, model: str) -> float:
    """Cost in USD for one call. Unknown models price at zero rather than
    guessing — a silently wrong cost figure is worse than an obvious gap."""
    rates = rates_for(model)
    if not rates:
        return 0.0

    fresh = usage.get("input_tokens", 0) or 0
    cached = usage.get("cache_read_input_tokens", 0) or 0
    written = usage.get("cache_creation_input_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0

    return (
        fresh * rates["input"] / 1_000_000
        + cached * rates["cached_input"] / 1_000_000
        + written * rates["input"] * CACHE_WRITE_MULTIPLIER / 1_000_000
        + output * rates["output"] / 1_000_000
    )


class TraceWriter:
    """Thread-safe JSONL writer. Concurrency is what makes 43k calls tractable,
    so the writer has to tolerate it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = self.path.open("a", encoding="utf-8")

    def write(self, record: TraceRecord) -> None:
        line = json.dumps(asdict(record), default=str, ensure_ascii=False)
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()


def summarise(path: Path) -> dict:
    """Roll up a trace file: spend, latency, decisions, failures."""
    source = Path(path)
    if not source.exists():
        return {"calls": 0}

    calls = 0
    errors = 0
    cost = 0.0
    latencies: list[int] = []
    decisions: dict[str, int] = {}

    for line in source.open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        calls += 1
        cost += row.get("cost_usd", 0.0)
        latencies.append(row.get("latency_ms", 0))
        if row.get("error"):
            errors += 1
        decision = row.get("decision")
        if decision:
            decisions[decision] = decisions.get(decision, 0) + 1

    latencies.sort()
    return {
        "calls": calls,
        "errors": errors,
        "cost_usd": round(cost, 4),
        "p50_latency_ms": latencies[len(latencies) // 2] if latencies else 0,
        "p95_latency_ms": latencies[int(len(latencies) * 0.95)] if latencies else 0,
        "decisions": dict(sorted(decisions.items(), key=lambda kv: -kv[1])),
    }
