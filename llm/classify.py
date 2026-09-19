#!/usr/bin/env python3
"""Classify unresolved entities as company or infrastructure.

Reads the dbt-built queue, calls the model once per entity with a versioned
prompt, and writes both a result table and a full JSONL trace.

Two properties matter more than throughput. It is resumable — already-classified
entities are skipped, so a killed run costs nothing. And `--dry-run` prices the
work before spending anything, which is the only way a token budget can be a
decision rather than a discovery.

Usage:
    python classify.py --dry-run
    python classify.py --limit 5000
    python classify.py --prompt-version v2 --limit 5000
"""

from __future__ import annotations

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
import duckdb
from pydantic import BaseModel, Field, ValidationError

from tracing import TraceRecord, TraceWriter, price_call, summarise

REPO = Path(__file__).resolve().parent.parent
WAREHOUSE = "/root/warehouse.duckdb"
RESULTS = REPO / "data" / "entity_classifications.jsonl"
TRACES = REPO / "data" / "traces" / "entity_classification.jsonl"

CLASSES = [
    "end_customer_company",
    "hosting_or_cloud",
    "cdn_or_security_vendor",
    "isp_telco",
    "government_or_education",
    "unknown",
]

MAX_ATTEMPTS = 3
CONCURRENCY = 8


class Classification(BaseModel):
    """Schema the model must fill. Enforced as a tool definition rather than
    parsed out of prose — an unparseable response is a failed call, not a
    silently mangled record."""

    entity_class: str = Field(description="One of the six permitted classes")
    canonical_name: str = Field(description="Best guess at the organisation's name")
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One sentence, citing the deciding evidence")


TOOL = {
    "name": "record_classification",
    "description": "Record the classification for this entity.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_class": {"type": "string", "enum": CLASSES},
            "canonical_name": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            # Measured at 288 output tokens per call without a length
            # constraint — the model writes paragraphs. Output is billed at 5x
            # input, so verbosity here was the single largest cost line.
            "reasoning": {
                "type": "string",
                "description": "At most 20 words. Cite the deciding evidence only.",
            },
        },
        "required": ["entity_class", "canonical_name", "confidence", "reasoning"],
    },
}


def load_prompt(version: str) -> tuple[str, str, str]:
    """Split a versioned prompt file into (model, system, user_template).

    Prompts live as files so v1 and v2 are diffable and every trace line can be
    attributed to the exact text that produced it.
    """
    path = REPO / "prompts" / version / "entity_classification.md"
    text = path.read_text(encoding="utf-8")

    model_match = re.search(r"^model:\s*(\S+)", text, re.M)
    model = model_match.group(1) if model_match else "claude-haiku-4-5-20251001"

    body = text.split("---", 2)[-1]
    system_part, user_part = body.split("# User", 1)
    system = system_part.replace("# System", "").strip()
    return model, system, user_part.strip()


def fetch_queue(limit: int | None) -> list[dict]:
    conn = duckdb.connect(WAREHOUSE, read_only=True)
    sql = "SELECT * FROM llm_classification_queue"
    if limit:
        sql += f" LIMIT {limit}"
    rows = conn.sql(sql).df().to_dict("records")
    conn.close()
    return rows


def already_done() -> set[str]:
    if not RESULTS.exists():
        return set()
    done = set()
    for line in RESULTS.open(encoding="utf-8"):
        try:
            done.add(json.loads(line)["entity_domain"])
        except (json.JSONDecodeError, KeyError):
            continue
    return done


def render(template: str, row: dict) -> str:
    """Fill the user template, truncating list fields. An entity with 200
    technologies would otherwise cost 10x one with five, for no extra signal."""
    def fmt(value):
        if isinstance(value, (list, tuple)):
            items = [str(v) for v in value if v][:12]
            return ", ".join(items) or "none"
        return "none" if value is None else str(value)

    filled = template
    for key in re.findall(r"\{(\w+)\}", template):
        filled = filled.replace("{" + key + "}", fmt(row.get(key)))
    return filled


class Classifier:
    """Owns the client, prompt version and trace writer for one run."""

    def __init__(self, version: str, tracer: TraceWriter):
        self.version = version
        self.tracer = tracer
        self.model, self.system, self.template = load_prompt(version)
        self.client = anthropic.Anthropic()

    def _call(self, row: dict) -> tuple[Classification | None, TraceRecord]:
        prompt = render(self.template, row)
        started = time.time()
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=400,
                    # Cache the system block: it is identical across all 43k
                    # calls and would otherwise dominate input spend.
                    system=[{"type": "text", "text": self.system,
                             "cache_control": {"type": "ephemeral"}}],
                    tools=[TOOL],
                    tool_choice={"type": "tool", "name": "record_classification"},
                    messages=[{"role": "user", "content": prompt}],
                )
                block = next(b for b in response.content if b.type == "tool_use")
                parsed = Classification(**block.input)
                usage = response.usage.model_dump()

                return parsed, TraceRecord(
                    task="entity_classification",
                    prompt_version=self.version,
                    model=self.model,
                    subject=row["entity_domain"],
                    decision=parsed.entity_class,
                    confidence=parsed.confidence,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cached_tokens=usage.get("cache_read_input_tokens", 0) or 0,
                    cost_usd=price_call(usage, self.model),
                    latency_ms=int((time.time() - started) * 1000),
                    attempt=attempt,
                    request={"prompt_chars": len(prompt)},
                    response=parsed.model_dump(),
                )

            except (ValidationError, StopIteration, anthropic.APIError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 ** attempt)

        return None, TraceRecord(
            task="entity_classification",
            prompt_version=self.version,
            model=self.model,
            subject=row["entity_domain"],
            decision=None,
            confidence=None,
            input_tokens=0, output_tokens=0, cached_tokens=0, cost_usd=0.0,
            latency_ms=int((time.time() - started) * 1000),
            attempt=MAX_ATTEMPTS,
            error=last_error,
        )

    def classify(self, row: dict) -> dict | None:
        parsed, trace = self._call(row)
        self.tracer.write(trace)
        if not parsed:
            return None
        return {"entity_domain": row["entity_domain"],
                "prompt_version": self.version, **parsed.model_dump()}


# Below this, cache_control is silently ignored and the block is billed at the
# full input rate. Discovered the hard way: the v1 system prompt was ~750
# tokens, every call reported cached_tokens=0, and nothing warned about it.
MIN_CACHEABLE_TOKENS = {"haiku": 2048, "sonnet": 1024, "opus": 1024}

# Markdown with punctuation and structure tokenises closer to 3.2 chars/token
# than the usual 4. The first estimator used 4 and came in 48% under.
CHARS_PER_TOKEN = 3.2

# Measured from real traces rather than assumed. The first estimate guessed 70
# and the actual was 288 — the reasoning field writes paragraphs unless the
# schema constrains it.
ASSUMED_OUTPUT_TOKENS = 290


def _cache_floor(model: str) -> int:
    for family, floor in MIN_CACHEABLE_TOKENS.items():
        if family in model:
            return floor
    return 2048


def estimate(rows: list[dict], version: str) -> None:
    """Price the run without making a call.

    Counts the tool schema, which is sent on every request and which the first
    version of this function ignored entirely, and reports whether the system
    block actually clears the cache threshold for the chosen model.
    """
    model, system, template = load_prompt(version)
    sample = rows[:200]
    if not sample:
        print("queue is empty")
        return

    avg_user = sum(len(render(template, r)) for r in sample) / len(sample) / CHARS_PER_TOKEN
    system_tokens = len(system) / CHARS_PER_TOKEN
    tool_tokens = len(json.dumps(TOOL)) / CHARS_PER_TOKEN
    cacheable_block = system_tokens + tool_tokens
    floor = _cache_floor(model)
    caching_works = cacheable_block >= floor

    n = len(rows)
    if caching_works:
        # First call writes the cache at a premium; the rest read it cheaply.
        usage = {
            "cache_creation_input_tokens": cacheable_block,
            "cache_read_input_tokens": max(0, n - 1) * cacheable_block,
            "input_tokens": n * avg_user,
            "output_tokens": n * ASSUMED_OUTPUT_TOKENS,
        }
    else:
        usage = {
            "input_tokens": n * (cacheable_block + avg_user),
            "output_tokens": n * ASSUMED_OUTPUT_TOKENS,
        }

    cost = price_call(usage, model)
    no_cache = price_call(
        {"input_tokens": n * (cacheable_block + avg_user),
         "output_tokens": n * ASSUMED_OUTPUT_TOKENS}, model)

    print(f"prompt version  : {version}")
    print(f"model           : {model}")
    print(f"entities        : {n:,}")
    print(f"system tokens   : {system_tokens:,.0f}")
    print(f"tool schema     : {tool_tokens:,.0f} (billed on every call)")
    print(f"cacheable block : {cacheable_block:,.0f} vs {floor:,} floor -> "
          f"{'CACHED' if caching_works else 'NOT CACHED, below minimum'}")
    print(f"user tokens     : {avg_user:,.0f} avg")
    print(f"output tokens   : {ASSUMED_OUTPUT_TOKENS} (measured from traces)")
    print(f"cost            : ${cost:,.2f}")
    print(f"cost if uncached: ${no_cache:,.2f}")
    print(f"per call        : ${cost / n:.5f}")


def run(args) -> None:
    rows = fetch_queue(args.limit)
    if args.dry_run:
        estimate(rows, args.prompt_version)
        return

    done = already_done()
    pending = [r for r in rows if r["entity_domain"] not in done]
    print(f"{len(rows):,} in queue, {len(done):,} already classified, "
          f"{len(pending):,} to do")
    if not pending:
        return

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    tracer = TraceWriter(TRACES)
    classifier = Classifier(args.prompt_version, tracer)
    written = 0

    with RESULTS.open("a", encoding="utf-8") as sink:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {pool.submit(classifier.classify, r): r for r in pending}
            for future in as_completed(futures):
                result = future.result()
                if not result:
                    continue
                sink.write(json.dumps(result, ensure_ascii=False) + "\n")
                sink.flush()
                written += 1
                if written % 250 == 0:
                    print(f"  {written:,}/{len(pending):,}")

    tracer.close()
    print(f"\nwrote {written:,} classifications to {RESULTS}")
    print(json.dumps(summarise(TRACES), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="price the run without calling the API")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--prompt-version", default="v1")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
