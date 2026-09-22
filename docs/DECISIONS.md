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

The rule tier removes 125,037 entities — five families, of which the provider
denylist is one — and the signal filter removes a further 83,464. An entity with no findings is not a prospect whether or not it is a
real company, so classifying it buys nothing. That filter is a boolean on an
aggregate — the alternative is a model call.

**Consequence.** Naive processing (a model call per source record) would cost
roughly $17,800 per pass. The designed path measures **$161** on-demand and
**~$80** batched — a reduction of 110× to 220×, driven mostly by a WHERE
clause.

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

## D12b — Not padding the prompt to reach the cache threshold

**Decision.** v2 ships without prompt caching. The remaining ~1,800 tokens
needed to clear Haiku 4.5's floor were not added.

**Why the floor was missed twice.** The minimum cacheable prefix is
model-dependent and **not monotonic across generations**:

| Model | Minimum |
|---|---:|
| Opus 5, Fable 5 | 512 |
| Opus 4.8, Sonnet 5, Sonnet 4.6 | 1,024 |
| Opus 4.7, Haiku 3.5 | 2,048 |
| **Haiku 4.5**, Opus 4.6, Opus 4.5 | **4,096** |

v1's block was 1,118 tokens. v2 was expanded to 2,308 on the assumption the
floor was 2,048 — it is 4,096 for this model, so caching still did not engage.
Both failures were silent: no error, no warning, only `cached_tokens: 0`.

**The arithmetic, per call:**

| Option | Cost/call | 43,577 entities |
|---|---:|---:|
| v2 as shipped, Haiku, no caching | $0.00340 | $148 |
| v2 padded past 4,096, Haiku, cached | $0.00125 | $54 |
| v2 as-is on Sonnet 5 (1,024 floor, caches) | $0.00214 | $93 |

**Why not pad.** Adding 1,800 tokens of prompt to clear a threshold — rather
than because the model needs the guidance — optimises the metric instead of the
system. The prompt is already long for a classification task, and the marginal
examples would be filler. Against a fixed deadline the hours are better spent
on the eval harness, which is the only thing that can say whether v2 is *better*
rather than merely cheaper.

**What was kept.** The output cap, which was the larger win and cost nothing:
288 → 136 tokens per call, latency roughly halved. Output is billed at 5×
input, so response verbosity was always the bigger lever.

**Recorded as an optimisation not taken**, with the numbers, because a
documented decision with arithmetic behind it is worth more here than the $17
it would have saved on the volume actually being run.

---

## D12c — Sonnet 5 over Haiku 4.5, chosen on quality because cost is a wash

**Decision.** v3 runs on Sonnet 5. Caching works there; it does not on Haiku
4.5, and the resulting costs are indistinguishable.

**The Haiku caching investigation.** Three attempts, each based on a wrong
number, each failing silently:

| Attempt | Block | Believed floor | Result |
|---|---:|---:|---|
| v1 | 1,118 | 2,048 | rejected |
| v2 | 2,313 | 2,048 | rejected — real floor is 4,096 |
| v3 | ~4,283 | 4,096 | **still rejected**, above the documented minimum |

Verified with three sequential identical requests:
`cache_creation_input_tokens: 0` on all three, full 4,380 tokens billed as
fresh input each time. Not a concurrency artifact and not a silent invalidator
— the prefix was byte-identical, evidenced by an identical token count on every
call.

The same prompt and the same code cache correctly on Sonnet 5 first time:
write 5,541, then reads of 5,541, input dropping to 347. So the implementation
was never wrong. Haiku 4.5 refused a block above its documented minimum, and
the investigation was stopped there rather than pursued further.

**Cost, measured:**

```
Haiku v2, uncached    2,731 in x$1  + 136 out x$5              = $0.00341
Sonnet v3, cached     5,541 read x$0.20 + 347 in x$2
                                       + 165 out x$10          = $0.00345
```

Caching on Sonnet exactly offsets Sonnet's higher token price. ~$27 per 8,000
entities either way.

**So the model choice is made on quality.** Sonnet is the stronger model, and
this task is a classification that is really a judgement — separating a
consultancy from a reseller means weighing contradictory evidence, not matching
a pattern. That is a deliberate deviation from the "cheap model for
classification" default, and the eval measures whether it was right rather than
leaving it as an assertion.

**Tokenisers differ per model.** Sonnet counts this prompt as 5,541 tokens
where Haiku counted ~4,283 — 30% more for identical text. Any chars-per-token
estimate is model-specific; trace data is authoritative and the dry-run
estimator should be read as a lower bound.

**Time cost of this investigation was disproportionate.** The decision moved
~$4 on the volume actually being run. It is recorded because the *method* —
instrument, measure, isolate with a minimal reproduction, stop when the answer
is economically irrelevant — is the transferable part, not the answer.

---

## D14 — Ship v3 on Sonnet 5, reversing D13

**Decision.** Production classification runs **v3 on Sonnet 5**. The 3,000
production classifications were re-run under it rather than left as they were.

**Why.** Four configurations against 75 hand-labelled entities, held-out batch
in bold — that column is the honest one, because v4's changes were written
after reading v3's failures on batch 1:

| Config | Accuracy | Precision, all 75 | **Precision, held out** | Cost/call |
|---|---:|---:|---:|---:|
| **v3 · Sonnet** | 0.667 | 0.615 | **0.667** | $0.00378 |
| v2 · Haiku | 0.653 | 0.588 | 0.500 | $0.00341 |
| v4 · Haiku | 0.667 | 0.524 | 0.467 | ~$0.00155 |
| v3 · Haiku | 0.627 | 0.471 | 0.385 | $0.00518 |

Sonnet also produces the fewest false positives on the headline class — 5,
against 7, 9 and 10 — which is the error that ends a sales call.

**This reverses D13, and the reversal is the point.** D13 shipped v3-on-Haiku
on 0.750 precision at n=25 and argued explicitly against Sonnet at 0.500, on
the reasoning that Sonnet "commits where Haiku hedges". At n=75 the ordering
inverts completely. Nothing changed but the number of labels.

D13 also recorded, at the time, that support of 6 made one row worth 0.25 of
precision and that only one comparison was defensible. That caveat was the only
reliable content in it.

**Two things follow, and both are worth more than the decision itself.**

A small eval does not fail loudly. It returns a clean table with three decimal
places and a plausible mechanism attached — the "higher accuracy, worse
product" story in D13 was coherent, memorable and wrong. Plausibility is not
evidence.

And a limitation is only worth writing down if you act on it. Having recorded
that n=25 could not rank configurations, the options on discovering the
reversal were to quietly keep the old recommendation or to redo the production
run. The run was redone, at about $11.

**What survives from D13.** The asymmetry still governs: a confident wrong
"company" is worse than an honest `unknown`, and that sets the confidence
threshold, the tiering gate and the upstream heuristics. What changed is which
model best satisfies it — a question the evidence, not the argument, decides.

---

## D12e — Fixed in v4, and the fix was found by measuring rather than reasoning

**Resolved and applied.** v4 caches on Haiku 4.5. Observed, not predicted:
write of 4,580 on a cold call, reads of 4,580 on the two following it, input
collapsing from 4,492 to 513.

**The diagnosis in D12d was right in substance and wrong in its number.** It
claimed v3's prefix was ~34 tokens short of 4,096. The real figure is closer to
**230 short** — v3's block measures around 3,867, which sits almost exactly on
the refusal boundary the probe found independently at ~3,875.

**How the wrong number arose is the part worth keeping.** I built
`measure_prefix.py` specifically to stop estimating, then estimated inside it:
it counted tokens for the whole request, subtracted a count of the message, and
called the difference the prefix. On v4 that method reported 4,895 where the
API's actual cached block was 4,580 — over-reporting by ~315 tokens. Applied to
v3 it reported 4,182, i.e. *86 tokens above* a floor the prompt was ~230 tokens
below.

So the tool built to end the estimating was wrong in the same direction, by
about the same margin, for the fourth time in a row.

**The fix, finally.** `measure_prefix.py` now makes one real call with
`cache_control` set and reports `cache_creation_input_tokens`. That value is the
cacheable block exactly, as the API accounts for it. A call costs a fraction of
a cent — less than being wrong about it again.

**Standing lesson.** Where a threshold is hard, do not compute the quantity —
observe it. Four attempts here failed identically: each derived a number that
was close enough to sound right and wrong enough to land on the wrong side of a
cutoff. The observation was always one API call away.

---

## D12d — Superseded: the prefix estimate that was itself an estimate

**Resolved.** Three iterations assumed the documented minimum was wrong. It was
not. Our cacheable prefix was roughly **34 tokens short of 4,096**, and a
chars-per-token estimate hid that.

**How it was settled.** A sweep across nine prefix sizes, run three ways —
without tools, with tools and a forced `tool_choice`, with tools and `auto` —
changing one variable at a time:

| Prefix | Verdict |
|---:|---|
| ~3,875 | refused |
| 4,203 | cached |
| 4,557 | cached |

Forced and automatic tool choice cached at identical sizes, so `tool_choice`
was never implicated.

**The mistake, precisely.** The 4,096 floor applies to the **cacheable prefix**
— tools plus system, everything before the breakpoint — not to total input. The
user message sits after the breakpoint and is never part of the cached block.
Measured on a cached call: total 4,983 = **4,557 prefix + 426 user message**.

v3 on Haiku reported 4,492 total input. Subtracting a user message of similar
size leaves a prefix near **4,062** — just under the threshold. I had estimated
that prefix at 4,283 from character counts and declared it clear. The estimate
was ~5% high, which was exactly enough to place a block below a hard threshold
while appearing to clear it.

**Why this kept recurring.** Every previous attempt reasoned from an estimate.
None measured. `cache_creation_input_tokens` on a cold call reports the prefix
exactly, and three rounds of argument could have been one probe.

**Not fixed in v3, deliberately.** Adding ~300 tokens would cross the floor and
cut cost from $0.00518 to roughly $0.00155 per call — a 3.3× reduction. But
that is a prompt change, and the eval measured v3 exactly as it stands.
Shipping an edited prompt while quoting the old prompt's precision would
invalidate the measurement, which is the same error avoided elsewhere in this
project. Recorded as costed work for v4, to be measured against a labelled set
built afterwards.

**Generalisable:** an estimate that is 5% wrong is harmless against a gradient
and fatal against a threshold. Where a hard cutoff exists, measure it.

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

---

## D15 — Ship at the cost ceiling rather than under it, with a named fallback

**Decision.** Ship v4 on Sonnet 5 at a measured **$0.00368 per call**, which
puts a full 43,577-entity refresh at **~$161** against a stated ceiling of
$150. Do not switch to a cheaper configuration to get under the line. Run the
queue on the **Batch API** — 50% of standard rates with caching still applied,
so ~$80 — which brings it comfortably under without touching accuracy.

**Why.** The configuration was chosen on precision, and precision on
`end_customer_company` is the metric that decides whether a rep trusts the
tool. Swapping to the cheaper configuration to satisfy a budget figure trades
the deployment metric for 3% of a number I set myself, early, before any of
the per-call costs had been measured.

The measured figures, from trace data rather than arithmetic:

| Config | $/call | Cached tokens/call | 43,577 entities |
|---|---:|---:|---:|
| v3 · Haiku 4.5 | $0.00504 | 0 — below the floor | $220 |
| **v4 · Sonnet 5** *(shipped)* | $0.00368 | 6,470 | **$161** |
| v3 · Sonnet 5 | $0.00355 | 5,519 | $155 |
| v4 · Haiku 4.5 | $0.00205 | 4,214 | $89 |
| v4 · Sonnet 5, batched | $0.00184 | 6,470 | **$80** |

**The ordering is the finding.** The shipped Sonnet configuration is cheaper
per call than *uncached Haiku*, despite a much higher list price. Caching
dominates model choice at this prompt size. It also means the original $57
estimate was never achievable: it implies $0.00131 per call, below every
configuration measured here, and the document carrying it recorded no per-call
figure to check it against.

**Consequence.** The ceiling now binds rather than providing headroom, and the
documented breach behaviour — degrade to rules-only tiering, queue the
remainder, alert — becomes a live path rather than a hypothetical one. Stated
plainly here instead of quietly re-basing the ceiling to $200, which would
have made the overrun disappear without changing anything real.
