# Known limitations

Open weaknesses and deliberate trade-offs, by layer. Written as it was built
rather than reconstructed afterwards, so it is honest about which were
considered decisions and which were time constraints.

**[trade-off]** chosen knowingly · **[gap]** would fix with more time ·
**[risk]** could produce wrong output

Solved problems are not mixed in with live ones — they are collected in
[Fixed along the way](#fixed-along-the-way) at the end, because several of them
taught more than the fix was worth.

---

## Start here

The six that change how you should read the output:

| | Why it matters |
|---|---|
| **Every CVE is version-inferred, never tested** | Shodan matches version banners against advisories. `verified` was false on 100% of 7,066 sampled entries. Scoring leads on EPSS instead, and outreach hedges. |
| **Tier A holds 818 of 1,005 accounts** | 81%. The queue is pre-filtered to entities with a finding, so urgency stops discriminating. Ranking within the tier is still correct; the label carries no information. |
| **The same prompt and model disagree with themselves on 4.5% of entities** | Measured across two identical runs of 3,000. It is the noise floor under every eval number here. |
| **The eval set is 75 examples, one labeller** | Support of 18 on the headline class, 12 held-out. It has already overturned its own recommendation twice. |
| **Classification precision is 0.765, recall 0.722** | Four false positives out of 75 would have reached a rep's call list. Two of the four are probably mislabelled. |
| **Only 3,000 of 43,577 queued entities are classified** | Budget. Everything below the cut stays `U - unclassified` and never reaches a rep. |

---

## Ingest (`ingest/ingest.py`)

**[risk] Shortest-domain-wins is a heuristic for entity ownership.**
`primary_domain()` picks the shortest registrable domain across a host's
hostnames. A host presenting `["cdn.acme.com", "x.io"]` resolves to `x.io`
purely because it is shorter, even if Acme owns the asset. Shortest is a proxy
for "closest to the apex", not a correct ownership rule. This is the specific
gap the LLM classification layer exists to cover, and the reason classification
precision is the metric that matters most.

**[gap] IPv6-only hosts carry no address.**
Shodan populates `ipv6` and leaves `ip_str` null for IPv6-only services —
106,419 records, 1.19% of the source. `ipv6` is not projected, so these hosts
have a null `ip`.

Measured rather than assumed: **81,945 of them (77%) have no entity anchor at
all** and are already dropped by `int_entity_hosts`, so they cost nothing. The
remaining **24,383 attribute to 3,570 entities** — 1.6% of 228,570 — and
contribute every signal normally, because entity resolution keys on hostnames
rather than addresses.

The residual cost is that `count(distinct ip)` would understate the estate for
those 3,570 entities, which feeds the ICP size band. Mitigated by falling back
to the first hostname in the distinct count. The app still cannot display an
address for them. A fifth re-ingest to capture `ipv6` was rejected: 40 minutes
of wall clock for a display field on 1.6% of entities, against a fixed deadline.

Found by a `not_null` test that was itself wrong — it asserted a property the
source does not have. Replaced with `assert_hosts_are_identifiable`, which
tests the weaker claim that actually matters: every host must have an address
*or* a name, or it can be neither attributed nor shown to a rep.

**[gap] ~26% of records have no resolvable domain.**
Excluded from entity grouping rather than attributed by IP or ASN. Returning
`NULL` instead of inventing a fallback is deliberate — a wrong entity anchor is
worse than a missing one — but a quarter of the raw data never reaches the
prospect list.

**[trade-off] Resume discards the in-flight shard.**
State is derived from the filesystem (count of completed shards) rather than a
separate state file, so nothing can drift out of sync. Cost: dying at record
450,000 loses the 50,000 rows buffered since the last flush. Acceptable for a
job that runs twice.

**[trade-off] Resume arithmetic assumes every existing shard is full.**
`shard_index * SHARD_SIZE` over-counts whenever the final shard is partial.
With atomic writes the dangerous case is gone; what remains is benign —
re-running a complete ingest over-skips and does nothing, observed as
`skipping 9,000,000` against 8,914,693 actual records. It would matter only for
an appended feed, which this design does not support. A per-shard row count in
a manifest would close it.

**[trade-off] Single-threaded parse.**
One core handles all 8.9M records. Parallelising requires splitting the zstd
frame, which is not cheaply seekable. For a source read twice the simple version
wins; a recurring pipeline would pre-split.

**[trade-off] `scanned_at` carried as a string.**
Cast happens in dbt staging so ingest stays a pure projection. Cost: no
time-based pruning at the Parquet layer. Irrelevant for a snapshot, wrong for an
incremental feed.

---

## Vulnerability data

**[risk] Every CVE finding is version-inferred, never confirmed.**
Shodan attaches CVEs by matching the detected product version against known
advisories — it does not test the host. Across 7,066 CVE entries in a
5,690-record sample, `verified` is `false` for **100%** of them. A host
reporting 99 CVEs on Apache 2.4.41 may have every one backported and patched;
the banner cannot tell us.

So raw CVE counts are close to meaningless as a ranking signal, and outreach
leading with "you have 99 vulnerabilities" is likely wrong and loses the rep
credibility on the first call. Mitigation: scoring leans on **EPSS** —
exploitation probability in the next 30 days — which is a published score about
the vulnerability rather than an inference about this host, so it survives the
same uncertainty. Severity language is hedged to what the banner supports: the
product and version are observed facts, the vulnerability is a possibility.

**[gap] CVSS alone would have produced a misleading ranking.**
Two sampled hosts both report max CVSS 9.8. One has a bug with EPSS 0.99999,
the other 0.01225 — identical severity, completely different urgency. A
CVSS-ordered list would have treated them as equivalent.

---

## Transformation (dbt)

**[gap] Scoring weights are hand-tuned, not fitted.**
Every component weight in `company_scores` is a judgement call. There is no
conversion data to fit against — no record of which accounts bought — so the
weights encode a plausible theory of urgency, not a measured one. Components
are kept as separate columns so they can be re-weighted against real outcomes
later, and so a rep can disagree with a ranking by seeing what drove it.

**[risk] Host count is a weak size proxy.**
The only size signal available, and it distorts both ways: a real company on
shared hosting exposes one host and looks tiny, while a company using many
subdomains looks large. The ICP size bands inherit that error directly.
Firmographic data would replace it.

**[risk] Certificate-derived entity keys can attribute to the wrong company.**
`int_entity_hosts` falls back to the certificate CN when there is no reverse DNS
record. A certificate can legitimately be issued for a domain hosted elsewhere,
so a cert-anchored host may belong to a different company than the one the
domain names. `entity_source` is carried through the pipeline so these can be
identified, filtered or weighted down — but they are currently treated the same
as hostname-anchored records.

**[gap] The seed list will never catch the long tail, by construction.**
Regional hosting providers are the largest source of false prospects and there
are thousands of them. Adding the ones visible in a 20-row sample would overfit
without generalising, so two shape-based heuristics were added instead —
sequential machine naming (`srv12.`, `vps-104.`) and port diversity
disproportionate to estate size. Both are deliberately weak; the LLM tier
absorbs what they miss.

**[gap] The seed provider list is manually curated and will go stale.**
~70 patterns covering the providers visible in this snapshot. New hosting
companies appear constantly and nothing refreshes the list. Production would
derive it from ASN ownership data rather than string matching.

**[risk] WAF posture is classified by regex over vendor strings.**
Distinguishing an incumbent security vendor from a CDN-bundled WAF drives the
whitespace score, and it is a pattern match over Shodan's vendor label. A new
vendor name, or a rename, silently falls through to `cdn_basic` and overstates
the opportunity.

**[gap] No cross-domain company resolution.**
`acme.com` and `acme.co.uk` are two companies. Merging them needs firmographic
data or an embedding match, both out of scope, so multinational estates are
fragmented across several entities.

**[trade-off] The infrastructure volume heuristic thresholds are arbitrary.**
`cloud_host_ratio >= 0.9 and n_hosts >= 50` catches providers absent from the
seed list. Both numbers were chosen by inspection, not tuned. Too loose and real
multi-cloud companies get classified as infrastructure; too tight and providers
leak into the prospect list. The LLM tier exists partly to absorb this
imprecision.

**[trade-off] `primary_country` uses the modal value.**
For a genuinely multinational estate the mode is close to arbitrary. The full
country list is retained alongside it so territory filtering can use either.

---

## LLM layer

**[risk] The same prompt and model disagree with themselves on 4.5% of
entities.**
A billing mistake left two independent classifications of the same 3,000
entities under the same prompt and model, which makes the disagreement between
them a direct measurement of run-to-run consistency:

| | |
|---|---:|
| Class agrees | 2,864 (95.5%) |
| Class flips | 136 (4.5%) |
| Tier changes | 96 |
| Mean absolute confidence movement | 0.023 |

The flips are not uniformly harmful — 25 move `unknown` to
`end_customer_company` — but not benign either: 19 move `end_customer_company`
to `unknown`, losing real prospects, and 4 move `hosting_or_cloud` to
`end_customer_company`, which is the failure that reaches a rep.

**This is the floor under every number in `evals/RESULTS.md`.** Held-out support
is 12, so one row moves precision by 0.083 and the gap between the best and
worst configuration is a few rows — the same order as a model's disagreement
with itself. The ranking is the best available evidence, not a stable property
of the models. A seed-controlled or repeated-run confidence interval is the
correct fix and is not implemented.

**[risk] Residual false positives concentrate in small regional ISPs and
hosting firms.**
Reading the top 20 of tier A: `korbank.pl` (Polish ISP and host), `lodz.pl`
(LODMAN, an academic network operator), `castle-it.net` (IT services) and
`e-pos.link` are all probably infrastructure, at confidence 0.60–0.75. Four in
twenty is consistent with the measured 0.765 precision, so the artifact matches
its own measurement rather than beating it.

`oracleoutsourcing.com` is a different case: Oracle is genuinely an end-customer
company, so the class is right, but hosts under an outsourcing domain likely
belong to Oracle's clients, making the findings attributable to someone else.
Correct classification, misleading evidence — a limitation of entity resolution,
not of the classifier.

Raising `MIN_CONFIDENCE` from 0.60 to 0.75 would clear three of the four and is
**deliberately not done**. Choosing a threshold after seeing which rows it
removes is the same error as tuning a prompt after seeing its eval.

**[risk] The labeller had evidence the model did not.**
Single-host entities with unfamiliar names — `bml.cz`, `bond.style`,
`tizbi.com`, `rajsyru.cz` — fail identically across every configuration. The
human resolved them by looking the companies up; the model sees one host, a
product string and an org name belonging to whoever owns the IP block.

Both readings are true: the scores understate the model relative to its inputs,
*and* they identify a real capability gap, since the product does need to
identify those companies. Which one you act on decides whether you tune the
prompt or go and find more evidence. The fix is more evidence — HTTP page
titles, WHOIS, or a search tool — not prompt tuning.

**[gap] The classifier only implements the synchronous path, which doubles the
bill.**
The Batch API processes identical requests at 50% of standard rates with prompt
caching still applied, and classification is the textbook workload for it:
43,577 independent requests, nothing waiting on any single one. A full refresh
is **~$80 batched against ~$161 on-demand**.

`classify.py` implements only the synchronous concurrent loop. That was the
right first choice — 25-entity tests and eval runs needed to return in seconds,
and batch turnaround is minutes to 24 hours — but it was never revisited when
the workload changed from "iterate on 25" to three successive runs of 3,000.
That cost about $16 of the $32 spent on classification.

Recorded as a gap rather than a trade-off, because `ARCHITECTURE.md` asserted
production would run the queue on a batch endpoint while the implementation
could not. A design that exists only in the prose is not a design.

**[gap] Only part of the queue is classified.**
43,577 entities qualify; the budget covers 3,000. The queue is ordered by fit so
a partial run covers the entities most likely to be real companies, but
everything below the cut stays `U - unclassified` and never reaches a rep.

**[gap] No human-review queue is wired up.**
Low-confidence classifications are recorded with their confidence score, but
there is no interface for a human to adjudicate them. The data supports it; the
workflow does not exist.

**[trade-off] Output verbosity is a deliberate cost.**
Output bills at 5× input, and `reasoning` was the largest single line in the
bill before being capped at 20 words — 159 output tokens are 43% of per-call
cost against 35% for 6,470 cached input tokens. Keeping it at all costs roughly
35% more than a bare label, paid for auditability: it is what makes a
classification reviewable against the labelled set.

---

## Evals

**[risk] One labeller, no adjudication.**
All 75 labels come from a single person with no second opinion and no measure of
inter-rater agreement. Where a label is wrong the model is penalised for being
right, and nothing in the process would surface it. Two labellers with
adjudicated disagreements is the standard fix and was skipped for time. At least
two labels are probably wrong: `3cx.ae` is a PBX *software* vendor labelled
`isp_telco`, and `digitalags.net` is similar.

**[risk] 75 is better than 25 and still small.**
Support on the headline class is 18 overall and 12 held-out, so the gap between
the top two configurations is a couple of rows. The current ordering is more
trustworthy than the previous one; it is not settled. 150+ examples with two
labellers would settle it.

**[gap] No inter-version significance testing.**
`run_eval.py` reports deltas between configurations but no confidence interval.
With support of 12 held-out, a single row moves precision by 0.06–0.08, so the
deltas invite over-reading. The report states this in prose; the harness should
state it in arithmetic.

**[gap] Prompt worked-examples are excluded, but nothing else is.**
`build_labelled_set.py` parses the prompt files and excludes any entity used as
a worked example, closing the obvious leakage path. Entities the prompt
describes *generically* — a named provider brand, a recognisable ccTLD pattern —
are not excluded and cannot easily be.

**[trade-off] The eval measures classification only.**
Scoring, tiering and outreach have no labelled ground truth. They are
deterministic and covered by dbt tests, so they are verifiable rather than
measurable — but "the tiering is sensible" is an assertion backed by reading
output, not a number.

---

## Application

**[risk] Tier A is too large to be a call list.**
Of 3,000 classified entities, 1,005 were confirmed organisations and **818 of
those — 81% — landed in tier A**. The cause is structural rather than a bad
threshold: the queue is already filtered to entities carrying at least one
security signal, so by the time scoring runs, intent is high for almost
everything that survives. `intent_score >= 50` no longer discriminates.

The app still ranks correctly within the tier, so the list is usable top-down,
but the tier label has stopped carrying information. The fix is to set the
threshold from the distribution after filtering, or to make tier a percentile
rather than an absolute cut. Not applied, because retuning thresholds to produce
a pleasing distribution after seeing the output is the same error as tuning a
prompt after seeing its eval.

**[gap] Outreach openers are template-generated, not model-written.**
The "what to say" text is assembled deterministically from the specific finding.
It is accurate and hedges correctly on version-inferred CVEs, but it reads the
same across accounts sharing a finding type. Format and tone controls select
between template families rather than reprompting. An LLM-drafted opener per
account is the obvious next increment, and would need its own eval to guarantee
the hedging that template assembly guarantees for free.

**[trade-off] The ranking skews heavily public sector.**
Nine of the top fifteen tier-A accounts are universities, research institutes or
government bodies. A genuine property of the data rather than a defect:
public-sector estates are large, old and heterogeneous, so they accumulate
findings, and their domains make them easy to identify confidently.

They are real buyers, but the motion is different — procurement and tenders, not
a cold call. Surfaced rather than suppressed, via a segment filter defaulting to
commercial-only, so a rep works one list or the other rather than a mixed one.

**[trade-off] The app reads a static snapshot.**
No database, no refresh, no auth. Correct for a demonstration and for the
batch-enrichment architecture, but production would need a scheduled rebuild of
the serving artifact and access control over it.

---

## Fixed along the way

Kept because the failure modes generalise, and because three of the four bugs
that mattered were found by **reading output, not by running tests**.

**Prompt caching failed silently, and four diagnoses all failed the same way.**
`cache_control` was placed correctly — identical code cached on Sonnet 5 — but
v3's cacheable prefix on Haiku 4.5 measured ~3,867 tokens against a **4,096**
floor. Requests succeeded, cache fields returned zero, nothing surfaced.

Two compounding errors. First, the floor applies to the *cacheable prefix*
(tools + system, everything before the breakpoint), not to total input — the
user message sits after the breakpoint and never joins the cached block, so
reading the floor against total input overstates the prefix. Second, and worse:
every attempt *derived* the prefix instead of observing it — from
chars-per-token, then by subtracting an estimated message size from an observed
total, then from a purpose-built measuring script that did the same subtraction
in tokens. That script over-reported by ~315 tokens, enough to report v3's
prefix as 86 *above* a floor it was ~230 *below*.

Each estimate was within ~5% and each landed on the wrong side of a hard cutoff.
`cache_creation_input_tokens` on a cold call reports the block exactly and costs
a fraction of a cent. Resolved in v4, whose 4,580-token prefix caches: cost fell
from $0.00518 to ~$0.00205 per call, so a **longer** prompt costs 2.5× less.
See `DECISIONS.md` D12d–D12e.

**The eval overturned its own recommendation twice.**
At n=25 this document reported 0.750 precision for v3-on-Haiku, ranked it first
of three, and argued against Sonnet at 0.500 on the reasoning that Sonnet
"commits where Haiku hedges". At n=75 the ordering inverted entirely — Haiku
last at 0.385 held-out, Sonnet first at 0.667. Nothing changed but the number of
labels; the reasoning was plausible and the evidence for it was noise.

Then a second reversal. v4 had only ever been evaluated on Haiku, because it was
built to clear Haiku's cache floor — so its three *content* changes were never
measured on the model actually shipping. Running it on Sonnet cost $0.28 and beat
v3-Sonnet on every metric, decisively on recall (0.444 → 0.722, five more real
companies out of 18) with fewer false positives.

Both recommendations were reversed and production re-run rather than leaving a
conclusion the evidence no longer supported. The n=25 caveat — support of 6, one
row worth 0.25 of precision, only one comparison defensible — was recorded
before the reversal and was the only reliable line in that table. That is the
argument for writing a limitation down when you notice it rather than after it
embarrasses you.

**A known prompt fix was held back, then applied and measured.**
`ax5z.com` carries `myra security` in its org list and every configuration
missed it — evidence present and unused. The fix was *not* applied at the time,
because changing a prompt after seeing an eval and then reporting that eval
turns a measurement into a fiction. v4 added the org-list vendor scan and was
scored against a set built after the change, with batch-1 and batch-2 reported
separately since v4's wording came from reading v3's batch-1 failures.

**An out-of-enum class silently removed a school from the market.**
`Classification.entity_class` was typed `str`. The permitted values lived only in
the tool schema's `enum`, which is advisory without `strict: true`, so Pydantic
accepted whatever came back. One call in 3,000 returned `education` instead of
`government_or_education` — `colegioanglomorumbi.com.br`, a Brazilian school,
correctly named, at confidence 0.9. The tier logic matches class names exactly,
so it fell through to `X - not a prospect` with nothing recording it. 1 in 3,000
is small enough to never notice and large enough to matter across 43,577.

Fixed at both layers, because the failure was that only one checked:
`entity_class` is now a `Literal`, so a stray value raises and the retry loop
treats it as a failed attempt; and `export_curated.py` refuses to export on any
unrecognised class rather than bucketing it. Found in a `decisions` histogram.

**Tiering treated "unclassified" as "is a prospect".**
The first build put hosting providers at the top of tier A — Beget, Forpsi,
startdedicated, vps-10, hostsila, plus the reverse-DNS zone `64.in-addr.arpa` —
all scoring 90 fit and 100 intent, because a multi-tenant estate accumulates
every finding belonging to everyone it hosts.

The scores were correct; the inference from them was not. Rules can prove an
entity **is** infrastructure but cannot prove it **is not**, and absence of
evidence was being read as evidence of absence. Fixed by gating tiers on
classification status. That asymmetry is the clearest justification for the
rule-vs-LLM split in the whole system, and it was found by reading twenty rows.

**The opener paired a product with a CVE that did not belong to it.**
`headline_cve_product` and `headline_cve` are both aggregated by
`arg_max(..., top_cve_epss)` — independently, so potentially from *different
services on the same host*. The draft asserted "you are running Apache httpd, a
version associated with CVE-2015-0235". CVE-2015-0235 is GHOST, a glibc bug.

Caught by reading three generated openers aloud. A wrong technical claim in a
first email is the fastest way for a rep to lose credibility, and it would have
been invisible to any test checking the text was non-empty. The opener now cites
the CVE and its exploitation probability, states plainly that it is inferred
from a banner, and asks the recipient to confirm rather than diagnosing. The
proper fix — carrying product and CVE through the aggregation as one struct so
they cannot be separated — is deferred.

**EPSS of 0.9997 printed as "100% chance of exploitation".**
Found by rendering 720 drafts while testing the tone controls. A certainty claim
in a cold email undoes the hedging the rest of the copy is built around. Now
capped at `>99%` and floored at `<1%`.

**IP addresses were becoming entities.**
Certificates can be issued to a bare IP, and the certificate-CN fallback
accepted them, so addresses with no organisation behind them entered the
pipeline — `155.159.120.87` appeared as a candidate company in a 25-row sample
built for the eval set. Nothing caught it because nothing could: the value is a
valid, non-null, correctly-typed string, so every schema test passed. Fixed by
rejecting IPv4 and IPv6 forms in staging, guarded by
`assert_entities_are_not_addresses`.

**Resume could silently skip a shard's worth of records.**
Each shard was written directly to its final filename. Parquet writes its footer
last, so a crash mid-write left a truncated file that the resume glob counted as
complete — skipping 100,000 records that were never saved. Found by accident:
reading the output directory during a run raised `No magic bytes found at end of
file part-00006.parquet`, the same state a crash would leave. Fixed by writing
to `.part-NNNNN.inflight` and atomically renaming on completion.

**The cost estimator understated by 5.7×.**
It omitted the tool schema sent on every request, used 4 chars/token against
markdown that tokenises nearer 3.2, and assumed 70 output tokens against a
measured 288. Corrected against real traces; it now also reports whether the
cacheable block clears the model's floor.

**The production run was paid for twice.**
The first 3,000-entity Sonnet run wrote results before `--model` was recorded,
so those rows carry `model: null`. The rerun's resume key — correctly — is the
`(prompt_version, model)` pair, which matched none of them, so all 3,000 were
classified again: 6,000 calls and $21.28 for 3,000 verdicts.

The resume logic was right and the data predated it. A key change is a
migration: rows written under the old key are invisible to the new one, and
"resume" silently becomes "restart". Both blocks were Sonnet, so the shipped
configuration was never in doubt — but which block's *verdicts* ship was, and
the two runs disagree on 136 of 3,000. That accident produced the
self-consistency measurement above.

**A real credit exhaustion mid-run cost 837 calls and zero entities.**
The account ran out of credit partway through a production run. 837 entities
exhausted all three retries and were written to the trace as errors — 6,000
successes plus 837 errors across 6,837 records — and every one was reclassified
on resume. Visible in the traces, invisible in the output, which is the correct
outcome and the first time the retry and resume paths were exercised by
something other than a test.

Worth stating because the alternative design fails silently: a classifier that
dropped failed entities rather than recording them would have produced a
prospect list quietly missing 837 accounts, with nothing to indicate it.
