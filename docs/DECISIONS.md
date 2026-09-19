# Decision log

Architectural decisions with the evidence that drove them, recorded as they
were made. Source material for the architecture document; kept separately so
the reasoning survives even where the final write-up has to be brief.

---

## D1 — Stream and project in one pass, never materialise the source

**Decision.** `ingest.py` decompresses the zstd source incrementally and writes
narrow Parquet shards. The decompressed JSON is never written to disk.

**Why.** The source is 12.36 GB compressed and ~74 GB decompressed (measured
ratio 6.03×). The processing machine has an 80 GB disk, of which 12.36 GB is
the source and ~3 GB the OS — about 64 GB free. **The decompressed dataset does
not fit on the machine that processes it.** `zstd -d` would fail outright.

**Consequence.** Memory stays flat regardless of input size, and the one
expensive read produces a reusable artifact. Scoring changes rerun against
Parquet in seconds instead of re-reading 74 GB.

---

## D2 — Entity key is the registrable domain, not the `org` field

**Decision.** Companies are identified by eTLD+1 extracted from reverse-DNS
hostnames, with the TLS certificate CN as a secondary anchor. The `org` field is
used only for matching against the infrastructure denylist.

**Why.** `org` names whoever registered the IP block, which for most records is
a hosting provider:

```
Google LLC                 2,745,419   30.8%
Incapsula Inc              1,778,843   20.0%
Cloudflare, Inc.             302,449
Amazon.com, Inc.             287,584
```

Grouping by `org` produces a prospect list whose top entries are Google,
Imperva and Amazon — worthless to a rep and immediately disqualifying.

**Consequence.** 8,914,693 records collapse to 252,078 entities. Domain coverage
is 73.5% from hostnames; the certificate CN recovers part of the remainder.

---

## D3 — Normalise org strings before matching

**Decision.** Lowercase, strip punctuation, drop legal suffixes before
comparing against the denylist.

**Why.** The source carries the same company under multiple spellings:

```
Incapsula Inc              1,778,843      Incapsula Inc.        61,958
Aliyun Computing Co., LTD    235,930      Aliyun Computing Co.LTD 78,039
METEVERSE LIMITED             78,354      Meteverse Limited.      62,896
```

Exact matching silently misses millions of records. This is the same company
name-matching problem the product itself exists to solve, appearing in the
source data.

---

## D4 — EPSS drives urgency, not CVE count and not CVSS

**Decision.** The headline finding per company is selected by EPSS
(exploitation probability within 30 days). CVSS and CVE counts are retained but
secondary.

**Why.** Two measured hosts both report max CVSS 9.8. One carries a bug with
EPSS 0.99999, the other 0.01225. Identical severity, completely different sales
urgency. A CVSS-ordered list treats them as equivalent.

Additionally, every CVE in this dataset is version-inferred: `verified` is
`false` across all 7,066 CVE entries sampled. So a host reporting 99 CVEs may
have every one patched. EPSS is a published score about the *vulnerability*
rather than an inference about *this host*, so it survives that uncertainty.

**Consequence.** Outreach can say "this bug is being actively exploited" rather
than "you have 99 vulnerabilities," which is both more useful and more likely
to be true.

---

## D5 — Rules can prove infrastructure, never absence of it

**Decision.** Tiering gates on classification status. Entities the rules cannot
resolve land in `U - unclassified` and cannot reach a rep's call list until the
LLM adjudicates.

**Why.** The first build ranked tier A as:

```
dizinc.com · startdedicated.com · forpsi.net · beget.com · hostsila.org
katapult.cloud · vps-10.com · fcsrv.net · 64.in-addr.arpa
```

Eighteen of twenty were hosting providers; one was a reverse-DNS zone. All
scored 90 fit and 100 intent, because a multi-tenant estate accumulates every
finding belonging to everyone it hosts.

The scores were correct. The inference was not: the logic read "rules found no
evidence of infrastructure" as "confirmed company." A seed match proves an
entity *is* a provider; nothing proves it *isn't*.

**Consequence.** This asymmetry is the clearest justification for the
rule-vs-LLM split in the system. The model call is paying for exactly the
inference the rules cannot make. Found by reading twenty rows of output, not by
reasoning about the design.

---

## D6 — Prefer an extra model call over a silent exclusion

**Decision.** The sequential-hostname heuristic threshold was raised from 0.6 to
0.8 after sampling.

**Why.** Two random samples of 25 excluded entities each contained one false
positive, both government bodies, both at the same score:

```
esteri.it     0.64   Italian Ministry of Foreign Affairs
hajnowka.pl   0.62   Polish municipality
```

Every unambiguous ISP sat at 0.79–1.00. The separation is explicable rather
than coincidental: municipal and ministry estates are small and hand-named,
while ISP customer-premises equipment is machine-named throughout.

**Cost of the change.** 355 entities returned to `unresolved`, of which 261
carry signals and join the model queue — a few cents.

**Consequence.** An excluded company is never reviewed and never recovers; an
extra classification costs a fraction of a cent. The asymmetry sets the
threshold, and the same reasoning is written into the classification prompt.

---

## D7 — The signal filter is a bigger cost lever than the denylist

**Decision.** The model queue requires both `rule_class = 'unresolved'` **and**
at least one security finding.

**Why.** Measured funnel:

```
8,914,693  source records
  252,078  entities                 (aggregation)
  127,041  unresolved               (rules removed 125,037)
   43,577  queued                   (signal filter removed 83,464)
```

The denylist removes 125,037 entities; the signal filter removes a further
83,464. An entity with no findings is not a prospect whether or not it is a
real company, so classifying it buys nothing. That filter is a boolean on an
aggregate — the alternative is a model call.

**Consequence.** Naive processing (a model call per source record) would cost
roughly $17,800 per pass. The designed path costs low tens of dollars: a
reduction of ~740×, driven mostly by a WHERE clause.

---

## D7b — Queue is ordered by fit, not intent

**Decision.** `llm_classification_queue` sorts by `fit_score desc`, then intent.

**Why.** Intent ordering was the first attempt and it was exactly backwards. A
multi-tenant estate accumulates every finding belonging to every tenant, so
sorting by urgency puts hosting providers at the front of the queue. Measured:
a 25-entity test run from the intent-ordered queue returned **20
`hosting_or_cloud`, 2 `isp_telco`, 2 `unknown`, and one real company**.

Fit is the better proxy for "worth asking about" — its size band peaks at
11–100 hosts and penalises estates above 500, which is the shape of a company
rather than a provider.

**Consequence.** Matters only because the budget is partial. If every entity
were classified the order would be irrelevant; because only the top slice gets
called, the sort key decides what the money buys.

---

## D12 — Cost estimates are worthless until validated against traces

**Decision.** The dry-run estimator is corrected against measured traces, and
reports whether caching will actually engage rather than assuming it.

**Why.** The first estimate was wrong by 5.7×, in three compounding ways:

| | Estimated | Measured | Cause |
|---|---|---|---|
| Input / call | 872 | 1,688 | tool schema uncounted; 4 chars/token too generous |
| Output / call | 70 | 288 | no length constraint on the `reasoning` field |
| Caching | engaged | **never** | system block below the model's minimum |
| Full queue | $24 | **$136** | all three compounding |

The caching failure is the one worth dwelling on. Anthropic's prompt cache has
a minimum cacheable length — 2048 tokens for Haiku. The v1 system block was
~1,118 tokens including the tool schema, so `cache_control` was **silently
ignored**. No error, no warning. The only evidence was `cached_tokens: 0` in
our own traces.

**Consequence.** Three things follow. Per-call trace logging is not optional —
it is the only place a silent pricing failure surfaces. Output tokens are
billed at 5× input, so verbosity in a response schema is a cost decision, not a
style one. And counter-intuitively, **making the system prompt longer makes the
run cheaper**: crossing the cache floor moves ~2,200 tokens from $1.00/M to
$0.10/M, which more than pays for the extra length. The natural way to add
those tokens is few-shot examples, which should improve accuracy at the same
time — that is the v1→v2 change.

---

## D8 — Fit and intent stay separate

**Decision.** Two independent 0–100 scores rather than one blended ranking,
crossed into a tier.

**Why.** Fit is structural and slow-moving (size, market, whether an incumbent
vendor is already present). Intent is event-driven and fast (an actively
exploited vulnerability, a cert expiring in three weeks). Blending them
destroys the distinction a rep actually works from: high fit with low intent is
a nurture sequence, low fit with high intent is a distraction, and only the
high/high quadrant earns a call today.

**Consequence.** Every contributing component is retained as its own column, so
a ranking can be explained rather than asserted. A rep who disagrees can see
what drove it.

---

## D9 — Absence of `security.txt` is not a signal; presence is

**Decision.** Treat publishing a `security.txt` as a positive maturity marker
rather than treating its absence as a negative.

**Why.** 8,907,026 of 8,914,693 records lack one — 99.91%. Absence has no
discriminating power. Only ~7,600 records have one, which makes presence a rare
and meaningful marker of a functioning security programme.

---

## D10 — Enrichment is batch; the app makes no model calls

**Decision.** Classification, briefs and outreach drafts are all precomputed.
The deployed app reads a static curated Parquet and holds no API key.

**Why.** Enrichment is a pipeline concern, not a request-path concern. Batch
precomputation is cheaper, removes secret management from deployment, makes the
hosted demo impossible to break with a rate limit, and is what a production
system would do anyway.

---

## D11 — Exclude honeypots and attacker infrastructure

**Decision.** Records tagged `honeypot`, or detected by RAT/malware scan
modules, are dropped before entity grouping.

**Why.** Honeypots are deliberate decoys — 48,383 records. The scan module
field also surfaces `remcos-pro-rat` and `darktrack-rat`, which are
attacker-operated command-and-control hosts.

**Consequence.** Neither is a prospect, and surfacing either would cost the rep
credibility on the first call. Guarded by a dedicated dbt test rather than
trusted to a WHERE clause.
