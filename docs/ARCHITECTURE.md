# Architecture

How the pieces fit, where the boundaries are and why they sit there, and what
was traded to put them there.

`TDD.md` is the design as specified. This is the system as built, with the
reasoning that survived contact with the data.

---

## The shape

```
shodan.json.zst ─► ingest ─► Parquet ─► dbt/DuckDB ─► LLM ─► curated ─► Streamlit
   12.4 GB        stream &   8.9M rows  rules, tests  entity  Parquet    hosted,
                  project 58            & scoring     class.  few k rows no API key
   ├──────────────── disposable compute instance ─────────────┤  ├─ laptop ─┤
```

Five stages, one direction, one artifact handed between each. The split down
the middle is deliberate: everything left of the handoff runs where the 12 GB
source lives and dies with that instance; everything right of it runs from a
few megabytes of derived business data.

## Why each boundary is where it is

### Ingest projects, and does nothing else

No scoring, no filtering, no deduplication, no business logic. It streams,
flattens and writes 58 columns.

The forcing constraint: the decompressed source is roughly **74 GB** and the
processing instance has about **64 GB free**. It does not fit. `zstd -d` fails
outright. So decompression is incremental and the JSON is never materialised —
memory stays flat regardless of input size.

The design consequence matters more than the constraint. Because ingest is a
pure projection, the one expensive read produces a reusable artifact. Every
subsequent change to scoring logic reruns against Parquet in seconds instead of
re-reading 74 GB. Pushing any business rule into this layer would have forfeited
that.

**Traded:** a single-threaded parse, because splitting a zstd frame is not
cheaply seekable. Forty minutes, twice. A recurring pipeline would pre-split.

### dbt owns every deterministic decision

Casting, normalisation, entity grouping, exclusions, signals, scoring, tiering.
Thirty-eight tests.

It lives here rather than in Python because these are set operations over
columns, they need to be readable by someone who did not write them, and they
need tests that run as part of the build rather than as a separate suite
somebody remembers to invoke.

Four of those tests guard failures that would actually cost something:
infrastructure reaching a rep's list, unclassified entities being treated as
prospects, honeypots surviving into the entity table, and scores drifting
outside their range. The rest are schema hygiene.

**Traded:** hand-tuned scoring weights. There is no conversion data to fit
against, so the weights encode a plausible theory of urgency rather than a
measured one. Every component is kept as a separate column specifically so they
can be re-weighted against real outcomes later, and so a rep can disagree with a
ranking by seeing what drove it.

### The LLM answers exactly one question

*Is this entity a company, or infrastructure?*

The boundary is set by an asymmetry rather than by taste. Rules can prove an
entity **is** infrastructure — a seed match, a reverse-DNS zone, machine-named
hosts across a large estate. **Nothing can prove it is not.** A regional hosting
provider the seed list has never heard of looks exactly like a small business
until someone reads the name.

So rules resolve the provable half and the model adjudicates the rest.
Everything it has not seen sits in `U - unclassified` and cannot reach a rep.

This was learned rather than designed. The first build treated "no evidence of
infrastructure" as "confirmed company", and tier A filled with hosting
providers — all scoring 90 fit and 100 urgency, because a multi-tenant estate
accumulates every finding belonging to every tenant. The scores were correct;
the inference drawn from them was not.

**Traded:** coverage. Only 3,000 of 43,577 queued entities were classified, on
budget. The queue is ordered by ICP fit so a partial run covers the entities
most likely to be real companies, but everything below the cut stays
unclassified and invisible.

### The app performs no inference

It reads a static Parquet. No database, no API key, no model call at request
time.

Enrichment is a pipeline concern, not a request-path concern. Precomputing it
is cheaper, removes secret management from deployment entirely, makes the
hosted demo impossible to break with a rate limit, and is what a production
system would do anyway.

**Traded:** freshness. The app shows a snapshot and nothing refreshes it.
Production would need a scheduled rebuild of the serving artifact.

---

## The rule-vs-LLM split, stated as a rule

> Use a rule wherever the answer is determined by structure. Use a model only
> where the answer requires reading meaning. Where a rule can settle it, a model
> call is slower, dearer and less auditable — and where a rule cannot, no amount
> of SQL will help.

Applied here:

| Decided by rule | Decided by model |
|---|---|
| CVE severity, EPSS ranking | Is this a company or a provider? |
| Port and service exposure | |
| Certificate expiry and validity | |
| TLS version acceptance | |
| End-of-life software | |
| Honeypot and C2 exclusion | |
| Known-provider denylist | |
| All scoring and tiering | |

**Where a model was deliberately not used**, and these were considered:

*Severity ranking.* CVSS and EPSS are published numbers. Asking a model to
judge severity would be slower, less consistent and wrong more often than
reading the field.

*Outreach copy.* Openers are assembled from the specific finding. A model would
write better prose, but the constraint here is accuracy — every claim must
trace to an observed fact, and hedging must be exact on version-inferred
vulnerabilities. Template assembly guarantees that; generation would need its
own eval to guarantee it. Logged as the next increment, not skipped by
accident.

*Entity grouping.* Extracting a registrable domain is a public-suffix lookup,
not a judgement.

---

## Cost model

```
Naive — one model call per source record
  8,914,693 records × ~2,000 tokens  = 17.8B input tokens    ≈ $17,800

Designed
  8,914,693 records
  →   252,078 entities        aggregation
  →   127,041 unresolved      rules removed 125,037
  →    43,577 queued          signal filter removed 83,464   ≈ $57
```

**The larger lever is the signal filter, not the denylist.** The provider
denylist removes 125,037 entities; requiring at least one security finding
removes a further 83,464. An entity with no findings is not a prospect whether
or not it is a real company, so classifying it buys nothing — and that filter
is a boolean on an aggregate, not a model call.

**Model routing.** Cheap model for classification at volume; a stronger model
would be reserved for per-account narrative generation on the top few hundred,
where output quality justifies a 5× output price.

**What the measurements changed.** The first cost estimate was wrong by 5.7×,
in three compounding ways: it ignored the tool schema sent on every request,
used a chars-per-token ratio that is model-specific, and assumed 70 output
tokens against a measured 288. Constraining the response schema to twenty words
halved output cost and latency. Prompt caching was pursued, measured, and found
to be refused by the chosen model above its documented minimum — the same code
caches correctly on another model, so the implementation was never wrong.
Caching would have saved about $4 at the volume actually run, and the
investigation was stopped there.

**Ceiling: $150 per full refresh.** On breach: degrade to rules-only tiering,
queue the remainder, alert. Estimates are validated against trace data rather
than assumed, because the first one was not.

---

## Observability

Every model call writes one line: task, prompt version, model, subject,
decision, confidence, input/output/cached/written tokens, cost, latency,
attempt, error.

That schema earned itself. Prompt caching failed silently on every call for
three iterations — no error, no warning, nothing in the response except
`cached_tokens: 0` in a field that existed only because the trace was built
before it was needed. A silent pricing failure has no other surface.

An earlier version of the schema recorded cache reads but not cache writes,
which made "never engaged" indistinguishable from "written every call, never
read" — two different bugs with two different fixes. Instrumentation has its own
blind spots.

---

## What this architecture is bad at

**Freshness.** One snapshot, no incremental path. `scanned_at` is carried as a
string because ingest does not cast, which is correct for a snapshot and wrong
for a feed.

**Entities below the classification cut.** 40,577 queued entities were never
classified. They are not wrong, they are absent — and absence is invisible in a
prospect list.

**Small companies.** The measured failure mode is single-host entities with
unfamiliar names. There is genuinely insufficient evidence in a scan record to
identify a small business, and no prompt fixes that. It needs page titles,
WHOIS or search — an evidence problem wearing a model problem's clothes.

**Anything requiring firmographics.** No headcount, no revenue, no industry.
Estate size is a weak proxy for company size and distorts in both directions.
