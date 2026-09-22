# Sales intelligence platform

**[Live app](https://sales-intelligence-platform-v7gqusfujwpubjxjjxgwz4.streamlit.app/)**

Turns 8.9 million internet-exposure scan records into a ranked, explainable
prospect list for a cybersecurity vendor's sales team. Deterministic rules do
the heavy lifting; an LLM is called for exactly one decision, and its accuracy
is measured.

---

## The problem, and why it isn't scoring

The dataset describes **machines**. A sales team needs **companies**. Two thirds
of those machines belong to hosting providers rather than to anyone you could
sell to:

```
Google LLC                 2,745,419   30.8%
Incapsula Inc              1,778,843   20.0%
Cloudflare, Inc.             302,449
Amazon.com, Inc.             287,584
```

Group by the ownership field and your top prospects are Google, Imperva and
Amazon — worthless to a rep and instantly disqualifying. Entity resolution is
the hard part; scoring is straightforward once you know who you're scoring.

## What it produces

A rep-facing list answering three questions per account — **why this account**,
**why now**, **what do I say** — filterable by territory, segment, estate size,
urgency, and whether an incumbent security vendor is already present.

From 8,914,693 records: 252,078 entities, 3,000 classified, **972 confirmed
organisations**, none of them infrastructure.

## Architecture

```
shodan.json.zst ─► ingest ─► Parquet ─► dbt/DuckDB ─► LLM ─► curated ─► Streamlit
   12.4 GB        stream &   8.9M rows  rules, tests  entity  Parquet    hosted,
                  project 58            & scoring     class.  few k rows no API key
```

| Layer | Does | Why there |
|---|---|---|
| Ingest | Streams zstd, projects 58 columns, writes Parquet | The decompressed source is ~74 GB and the processing box has 64 GB free — it does not fit, so it is never materialised |
| dbt on DuckDB | Casts, groups to entities, excludes, scores | All business logic in one tested place; rescoring reruns in seconds instead of re-reading 74 GB |
| LLM | Classifies entities rules cannot resolve | The only question rules cannot answer |
| Streamlit | Filters and explains a precomputed Parquet | No inference at request time, no credentials in deployment |

## The rule-vs-LLM split

**Rules** handle everything structural: CVE severity, port exposure, certificate
expiry, TLS versions, end-of-life software, honeypot and malware exclusion, the
provider denylist, scoring and tiering. 38 dbt tests.

**The LLM** answers one question: *is this entity a company or infrastructure?*

That boundary exists because of an asymmetry. Rules can prove an entity **is**
infrastructure — a seed match, a reverse-DNS zone, machine-named hosts. Nothing
can prove it **is not**. Unresolved entities sit in `U - unclassified` and never
reach a rep until the model adjudicates.

The first build got this wrong: tiering treated "no evidence of infrastructure"
as "confirmed company", and tier A filled with hosting providers, all scoring 90
fit and 100 urgency because a multi-tenant estate accumulates every finding
belonging to every tenant. The scores were right; the inference was not.

## Cost

```
Naive — one model call per source record            ≈ $17,800
Designed —
  8,914,693 records
  →   252,078 entities      aggregation
  →   127,041 unresolved    rules removed 125,037
  →    43,577 queued        signal filter removed 83,464    ≈ $155
```

The larger lever is the **signal filter**, not the rules. An entity with no
findings is not a prospect whether or not it is a real company, so classifying
it buys nothing — and that filter is a `WHERE` clause, not a model call.

Five rule families remove those 125,037, and the provider denylist is only one
of them — `reverse_dns_zone`, `seed_list`, `cloud_tag_volume`,
`sequential_hostnames`, `port_diversity`. The heuristics exist because a
curated list of names cannot reach the long tail of regional hosts.

That $155 is measured rather than projected — the production run's own traces —
but it is **on-demand** pricing, and the two levers that matter are both larger
than the model choice:

| | Per full refresh |
|---|---:|
| Sonnet, on-demand *(as actually run)* | ~$161 |
| **Sonnet, Batch API — 50%, caching still applies** | **~$80** |
| Haiku, batched | ~$45 |

Caching is the first lever: the shipped Sonnet configuration is *cheaper per
call than uncached Haiku*, despite triple the list price. Batching is the
second, and `classify.py` implements only the synchronous path — a gap recorded
in [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md), since the
architecture doc claimed batch and the code could not do it.

Production ceiling: **$150 per full refresh** — comfortable batched, ~7% over
as executed. On breach, degrade to rules-only tiering, queue the remainder,
alert.

## Measured quality

v3 on Sonnet 5, against 75 hand-labelled entities:

| | |
|---|---|
| Precision, `end_customer_company` | **0.615** (0.667 on held-out batch) |
| Recall | 0.444 |
| Accuracy | 0.667 |

Precision on that class is the metric that governs deployment: a false positive
puts a hosting provider in a call list and the tool loses the rep's trust. A
false negative removes a company from the market invisibly.

**The most useful thing this eval did was overturn its own earlier conclusion.**
At 25 labels it ranked v3-on-Haiku first at 0.750 precision and argued against
Sonnet at 0.500. At 75 labels that ordering completely inverts — Haiku falls to
0.385 on held-out data and Sonnet leads at 0.667. Nothing changed but the
number of labels.

The caveat was written before the reversal: the n=25 report stated that support
of 6 made one row worth 0.25 of precision and that only one comparison was
defensible. That turned out to be the only reliable line in the table. The
recommendation was reversed and the production run redone on Sonnet.

Full analysis, the reversal, and what still doesn't hold at n=75 in
[`evals/RESULTS.md`](evals/RESULTS.md).

## Running it

**The app**, against the committed Parquet:

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app/streamlit_app.py
```

**The pipeline** needs the source dump and an Anthropic key, and is intended to
run on a disposable instance rather than a laptop:

```bash
pip install -r ingest/requirements.txt
python ingest/ingest.py shodan.json.zst ./parquet     # ~40 min, resumable
python ingest/validate.py ./parquet                    # profile the output
cd dbt && DBT_PROFILES_DIR=. dbt seed && dbt build     # 38 tests
cd ../llm && python classify.py --dry-run              # price before spending
python classify.py --limit 3000 --prompt-version v3
cd .. && python export_curated.py                      # serving artifact
```

**The evals:**

```bash
python evals/build_labelled_set.py 25   # worksheet, labels blank
python evals/merge_labels.py            # merge plain-text labels
python evals/run_eval.py --prompt-version v3 --model claude-haiku-4-5
```

## Layout

| Path | |
|---|---|
| `ingest/` | streaming ingest, output profiler, raw-field inspector |
| `dbt/` | staging → entity → signals → scoring, with 38 tests |
| `prompts/v1..v4/` | versioned prompts; every trace names the pair that produced it |
| `llm/` | classifier, trace schema, cost estimator, cache diagnostics |
| `evals/` | labelled set, harness, per-configuration results |
| `skills/` | `entity-classification/SKILL.md` |
| `app/` | Streamlit app and its curated Parquet |
| `docs/` | TDD, decision log, known limitations |

## Documents

- [`docs/PLANNING.md`](docs/PLANNING.md) — use cases chosen and why
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the pieces fit, the rule-vs-LLM split, the cost model, and what was traded
- [`docs/TDD.md`](docs/TDD.md) — technical design, in the format my team uses
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — decision log with the evidence behind each
- [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) — open weaknesses by layer
- [`docs/HOW_I_BUILT_THIS.md`](docs/HOW_I_BUILT_THIS.md) — dev loop, where AI helped and where it cost time
- [`evals/RESULTS.md`](evals/RESULTS.md) — measured quality and its caveats
- [`NOTES.md`](NOTES.md) — build journal, written as it happened

## Known weaknesses

Kept in full in [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md). The
three worth knowing before reading anything else:

**Every CVE finding is version-inferred.** Shodan matches detected versions
against advisories; it does not test the host. `verified` was false on 100% of
7,066 sampled CVE entries. Scoring therefore leads on **EPSS** — a published
probability that a vulnerability is being exploited — rather than CVE counts,
and generated outreach hedges accordingly.

**The eval set is still small.** 75 examples, support of 18 on the headline
class and 12 on the held-out batch, one labeller, no adjudication. At 25 it was
small enough to produce a confidently wrong ranking and did. At 75 the ordering
is more trustworthy, not settled — the gap between the top two configurations
is a couple of rows.

**Tier A holds 779 accounts, which is not a call list.** The queue is already
filtered to entities with a finding, so urgency is high for nearly everything
that survives and the threshold stops discriminating. Documented rather than
retuned: adjusting a threshold after seeing the distribution, to produce a
nicer split, is the same error as tuning a prompt after seeing its eval.
