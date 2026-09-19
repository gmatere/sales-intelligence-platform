# TDD — AI Sales Intelligence Platform

**Author** Gouri Matere · **Status** In build · **Target** Tue 22 Sep 2026

---

## Goal

Give a salesperson at a cybersecurity vendor a ranked, explainable list of
companies that need their product **now**, built from internet-exposure scan
data.

The hard part is not scoring. It is that the dataset describes *machines*, and
a sales team needs *companies* — and two thirds of those machines belong to
hosting providers rather than to anyone you could sell to.

## Non-goals

- Firmographic enrichment (headcount, revenue, funding) — not in the dataset
- CRM write-back or sequence automation
- Real-time scanning; this is a single 2026-09-14 snapshot
- Cross-domain company merging (`acme.com` and `acme.co.uk` stay separate)

## Dataset

Shodan internet-wide scan banners. One record per exposed service.

| | |
|---|---|
| Source | 12.36 GB zstd, ~74 GB decompressed JSONL |
| Records | 8,914,693 across 3,087,232 IPs |
| Geography | US 49%, Singapore 21%, then CN / DE / GB / JP |
| Key fields | ports, products, versions, TLS, WAF vendor, CVEs with CVSS + EPSS, Shodan tags |

The decompressed data does not fit on the machine that processes it, so ingest
streams and projects in a single pass.

## Architecture

```
shodan.json.zst ──► ingest ──► Parquet ──► dbt/DuckDB ──► LLM ──► curated ──► Streamlit
   12.4 GB         stream &    8.9M rows   rules, tests   entity   Parquet     hosted,
                   project 58              & scoring      class.   few k rows  no API key
```

| Layer | Responsibility | Why there |
|---|---|---|
| **Ingest** | Stream-decompress, project 58 columns, write Parquet shards | One expensive read; resumable; never materialises 74 GB |
| **dbt on DuckDB** | Cast, normalise, group to entities, exclude, score | All business logic in one testable place; rerun in seconds |
| **LLM** | Classify entities rules can't resolve | The only question rules cannot answer |
| **Serving** | Precomputed Parquet → Streamlit | No runtime inference, no secrets in deployment |

Everything heavy runs on a disposable cloud instance. The laptop holds source
code and the final curated export only.

## The rule-vs-LLM split

**Rules** handle everything structural: CVE severity, port exposure, cert
expiry, TLS versions, EOL software, honeypot and malware exclusion, known-
provider denylist, scoring, tiering. Cheap, exhaustive, auditable.

**The LLM** answers one question: *is this entity a company or infrastructure?*

That boundary exists because of an asymmetry. Rules can prove an entity **is**
infrastructure — a seed match, a reverse-DNS zone, machine-named hosts. Nothing
can prove it **is not**. Entities the rules cannot resolve are held in
`U - unclassified` and never reach a rep until the model adjudicates.

## Scoring

Two independent scores, not one blend. **Fit** (structural: size band,
whitespace, market) crossed with **Intent** (urgent: EPSS, exposed datastores,
expiring certs) gives the tier a rep works from — A call now, B nurture, C
opportunistic, D deprioritise.

Urgency is driven by **EPSS**, not CVE count or CVSS. Every CVE here is
version-inferred (`verified` is false on 100% of them), so counts are
meaningless; EPSS is a published exploitation probability about the
vulnerability rather than an inference about the host.

## Cost model

```
Naive — one model call per source record
  8,914,693 × ~2,000 tokens                        ≈ $17,800

Designed — rules and signal filters collapse first
  8,914,693 records
  →   252,078 entities        aggregation
  →   127,041 unresolved      denylist + heuristics removed 125,037
  →    43,577 queued          signal filter removed 83,464
                                                   ≈ $119 measured
```

The larger lever is the **signal filter**, not the denylist: an entity with no
findings is not a prospect whether or not it is a real company.

**Model routing** — cheap model for classification at volume, stronger model
for account briefs on the top few hundred only.

**Production ceiling** — $150 per full refresh. On breach: degrade to
rules-only tiering, queue the remainder, alert. Estimates are validated against
trace data, never assumed; the first estimate was 5.7× low.

## Build sequence

| | Step | State |
|---|---|---|
| 1 | Ingest → Parquet, 58 columns, resumable | done |
| 2 | dbt layer: staging → entity → signals → scores, 38 tests | done |
| 3 | Classifier: versioned prompt, structured output, tracing, dry-run | done |
| 4 | Prompt v2 — few-shot, crosses cache floor, constrains output | next |
| 5 | Labelled set (25) + eval harness + v1 vs v2 | next |
| 6 | Enrichment run, account briefs, outreach drafts | |
| 7 | Curated export → Streamlit → deploy | |
| 8 | SKILL.md, planning doc, architecture doc, How You Build | |

## Risks

| Risk | Mitigation |
|---|---|
| Hosting providers reach a rep's call list | Tier gating + dedicated dbt test; the one failure that destroys trust |
| Real companies silently excluded | Heuristic thresholds tuned against sampled false positives; prefer an extra model call |
| CVE claims are wrong | EPSS over counts; briefs hedge to what the banner supports |
| Classification quality unmeasured | Eval harness before any quality claim is made |

## References

`docs/DECISIONS.md` — decision log with evidence ·
`docs/KNOWN_LIMITATIONS.md` — open weaknesses by layer ·
`NOTES.md` — build journal
