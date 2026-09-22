# DE Technical Design Document — AI Sales Intelligence Platform

## Summary

Give a salesperson at a cybersecurity vendor a ranked, explainable list of
companies that need their product **now**, derived from internet-exposure scan
data.

The hard part is not scoring. The dataset describes *machines*; a sales team
needs *companies* — and two thirds of those machines belong to hosting
providers rather than to anyone you could sell to. Entity resolution is the
problem; scoring is the easy part downstream of it.

**Owner** · Gouri Matere
**Reviewers** · Firmable engineering
**Stakeholders** · Sales (hypothetical end user)
**Status** · DRAFT — target 22 Sep 2026

---

## Scope

Ingest an 8.9M-record Shodan snapshot, resolve host records to company
entities, derive security-posture signals, score and tier accounts, and serve
them through a hosted app with per-account briefs and outreach drafts.

LLM use is confined to one decision — company vs infrastructure — with full
production scaffolding around it: versioned prompts, structured outputs,
per-call tracing, a labelled eval set, and a measured cost model.

### Out of scope

- Firmographic enrichment (headcount, revenue, funding) — absent from the source
- CRM write-back or outreach sequence automation
- Real-time scanning; this is a single 2026-09-14 snapshot
- Cross-domain company merging (`acme.com` and `acme.co.uk` stay separate)
- Human-review UI for low-confidence classifications (data supports it, workflow not built)

### Impact

Without entity resolution the dataset is unusable for sales: a naive ranking
returns Google, Cloudflare and Amazon. The value is entirely in separating the
~127k plausible companies from the ~125k providers, then ranking by urgency
that a rep can defend on a call.

---

## High-Level Architecture

### Technical diagram

```
shodan.json.zst ──► ingest ──► Parquet ──► dbt/DuckDB ──► LLM ──► curated ──► Streamlit
   12.4 GB         stream &    8.9M rows   rules, tests   entity   Parquet     hosted,
                   project 58              & scoring      class.   few k rows  no API key
```

| Layer | Responsibility | Why here |
|---|---|---|
| Ingest | Stream-decompress, project 58 columns, Parquet shards | One expensive read; resumable; never materialises 74 GB |
| dbt on DuckDB | Cast, normalise, group to entities, exclude, score | All business logic in one testable place |
| LLM | Classify entities rules cannot resolve | The only question rules cannot answer |
| Serving | Precomputed Parquet → Streamlit | No runtime inference, no secrets in deployment |

The decompressed source (~74 GB) does not fit on the machine that processes it
(64 GB free), so ingest streams and projects in a single pass.

### Producers & consumers

**Producer** · Shodan scan export, one record per exposed service.
**Consumers** · Streamlit app (primary); `docs/` artefacts; eval harness reads
the same trace schema.

### Rule vs LLM split

**Rules** handle everything structural: CVE severity, port exposure, cert
expiry, TLS versions, EOL software, honeypot and malware exclusion, provider
denylist, scoring, tiering.

**The LLM** answers one question: *is this entity a company or infrastructure?*

That boundary exists because of an asymmetry. Rules can prove an entity **is**
infrastructure — seed match, reverse-DNS zone, machine-named hosts. Nothing can
prove it **is not**. Unresolved entities are held in `U - unclassified` and
never reach a rep until the model adjudicates.

### Error / failure handling

Ingest is resumable with atomic shard writes, so a crash cannot leave a
truncated shard that resume counts as complete. Classification retries three
times with backoff, records failures to the trace with the error text, and
skips already-classified entities on rerun. Malformed source lines are counted
and reported, not silently dropped.

---

## Data Quality & Validation

### Dimensions

**Accuracy** (entity attribution is the whole product) · **Validity** (scores
in range, tiers from a fixed set) · **Uniqueness** (one row per entity) ·
**Completeness** (identifiability of host records).

### Checks and validations

48 dbt tests. Column-level: `not_null`, `unique`, `accepted_values` on
transport, entity source, rule class and tier. Four singular tests guard the
failures that would actually cost something:

| Test | Guards against |
|---|---|
| `assert_infrastructure_never_tiered` | A CDN reaching a rep's call list |
| `assert_unclassified_never_tiered` | Treating "no evidence" as "confirmed company" |
| `assert_no_unsellable_entities` | Honeypots and C2 hosts surviving into entities |
| `assert_scores_within_bounds` | Score drift outside 0–100 breaking tier thresholds |

`assert_hosts_are_identifiable` runs at **warn** severity with an error
threshold above the known 81,945 baseline, so a regression breaks the build
while the known figure stays visible.

### Monitoring and SLAs

Quality gates run inline with `dbt build`; a failing test blocks all downstream
models. For a single-snapshot prototype there is no recurring SLA. In
production the thresholds above become the alerting contract, with the
unidentifiable-host count as a source-shape regression detector.

---

## Non-Functional Requirements

### Observability

Every model call writes one JSONL line: task, prompt version, model, subject,
decision, confidence, input/output/cached tokens, cost, latency, attempt,
error. That schema is what surfaced a silent prompt-cache failure that produced
no error of any kind.

### Performance

Ingest ~40 min single-threaded over 8.9M records. dbt build under 2 min.
Classification at 8 concurrent workers, ~3s p50.

### Estimated costs

```
Naive — one model call per source record        ≈ $17,800
Designed —
  8,914,693 records
  →   252,078 entities     aggregation
  →   127,041 unresolved   rules removed 125,037
  →    43,577 queued       signal filter removed 83,464   ≈ $119 measured
```

Larger lever is the **signal filter**, not the denylist: an entity with no
findings is not a prospect regardless of what it is.

**Model routing** · cheap model for classification at volume, stronger model for
account briefs on the top few hundred.
**Ceiling** · $150 per full refresh. On breach: degrade to rules-only tiering,
queue the remainder, alert.

Estimates are validated against traces, never assumed — the first estimate was
5.7× low.

### Security

Source data is public internet scan data; no PII or PHI. All processing on a
disposable instance; API key in a `chmod 600` file outside the repo. The
deployed app holds no credentials because all inference is precomputed. Raw IPs
are excluded from the serving artefact — a rep needs the company and the
finding, not an address.

---

## Dependencies & Deployment

Python 3.12, DuckDB, dbt-duckdb, pyarrow, zstandard, tldextract, Anthropic SDK.
Ingest, transform and enrichment run on a cloud instance; the app runs from the
curated Parquet and deploys to Streamlit Community Cloud from the repo.

---

## Rollout Plan / Milestones

| | Milestone | State |
|---|---|---|
| 1 | Ingest → Parquet, 58 columns, resumable | done |
| 2 | dbt layer: staging → entity → signals → scores, 48 tests | done |
| 3 | Classifier: versioned prompt, structured output, tracing, dry-run | done |
| 4 | Prompt v2 — few-shot, crosses cache floor, constrains output | next |
| 5 | Labelled set (25) + eval harness + v1 vs v2 | next |
| 6 | Enrichment run, account briefs, outreach drafts | |
| 7 | Curated export → Streamlit → deploy | |
| 8 | SKILL.md, planning doc, architecture doc, How You Build | |

---

## Alternative Designs Considered

**Group by `org` instead of registrable domain.** Rejected — `org` names the IP
block registrant, which is Google for 30.8% of records. Produces a prospect
list of cloud providers.

**LLM over raw records.** Rejected on cost (~$17,800/pass) and quality: most of
the work is deterministic and a model answers those questions worse than SQL.

**Single blended score.** Rejected — collapses fit and intent, which are the two
axes a rep actually works from. High-fit/low-intent is a nurture sequence, not
a weaker version of a call-now account.

**Rank by CVSS or CVE count.** Rejected — every CVE here is version-inferred
(`verified` false on 100%). Two hosts both at CVSS 9.8 had EPSS of 0.99999 and
0.01225: identical severity, completely different urgency.

**Runtime LLM calls from the app.** Rejected — needs secrets in deployment,
costs per page view, and breaks under a rate limit during a demo.

---

## Test Plan

dbt tests cover the deterministic layer. The non-deterministic layer is covered
by a hand-labelled set of 25 entities and a one-command eval harness reporting
precision and recall per class against the previous prompt version.

Precision on `end_customer_company` is the headline metric: a false positive
puts a hosting provider in a call list and the rep stops trusting the tool.

---

## Appendix

### Open questions

- Should market scope be global or restricted to specific territories?
- What classification precision is acceptable before a list goes to a rep?

### Resources

`docs/DECISIONS.md` — decision log with evidence ·
`docs/KNOWN_LIMITATIONS.md` — open weaknesses by layer ·
`NOTES.md` — build journal
