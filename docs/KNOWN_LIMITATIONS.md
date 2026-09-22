# Known limitations

Deliberate trade-offs and open weaknesses in this system, by layer. Written as
it was built rather than reconstructed afterwards, so the reasoning is honest
about what was a considered decision versus what was a time constraint.

Legend: **[trade-off]** chosen knowingly · **[gap]** would fix with more time ·
**[risk]** could produce wrong output

---

## Ingest (`ingest/ingest.py`)

**[risk] Shortest-domain-wins is a heuristic for entity ownership.**
`primary_domain()` picks the shortest registrable domain across a host's
hostnames. A host presenting `["cdn.acme.com", "x.io"]` resolves to `x.io`
purely because it is shorter, even if Acme owns the asset. Shortest is a proxy
for "closest to the apex", not a correct ownership rule. This is the specific
gap the LLM entity-classification layer exists to cover, and the reason
classification precision is the metric that matters most.

**[trade-off] Resume discards the in-flight shard.**
State is derived from the filesystem (count of completed shards) rather than a
separate state file, so there is nothing to drift out of sync. Cost: dying at
record 450,000 loses the 50,000 rows buffered since the last flush. Acceptable
for a job that runs twice.

**[fixed] Resume could silently skip a shard's worth of records.**
The first implementation wrote each shard directly to its final filename.
Parquet writes its footer last, so a crash mid-write left a truncated file on
disk — which the resume glob counted as complete, skipping 100,000 records
that were never saved. Found by accident: reading the output directory during
a run raised `No magic bytes found at end of file part-00006.parquet`, which
is the same truncated state a crash would leave behind.

Fixed by writing to `.part-NNNNN.inflight` (excluded from the resume glob) and
atomically renaming on completion. An interrupted run now leaves an ignored
temp file rather than a fake-complete shard.

**[trade-off] Resume arithmetic assumes every existing shard is full.**
`shard_index * SHARD_SIZE` over-counts whenever the final shard is partial.
With atomic writes the dangerous case is gone, and what remains is benign: re-
running an already-complete ingest over-skips and does nothing. Observed as
`skipping 9,000,000` against 8,914,693 actual records. It would matter only if
the source grew between runs (an appended feed rather than a snapshot), which
this design does not support. A per-shard row count in a manifest would close
it properly.

**[trade-off] Single-threaded parse.**
One core handles all 8.8M records. Parallelising requires splitting the zstd
frame, which is not cheaply seekable. For a source read twice, the simple
version wins; a recurring pipeline would pre-split the input.

**[trade-off] `scanned_at` carried as a string.**
Cast happens in dbt staging so ingest stays a pure projection. Cost: no
time-based pruning at the Parquet layer. Irrelevant for a single snapshot,
wrong for an incremental feed.

**[gap] IPv6-only hosts carry no address.**
Shodan populates `ipv6` and leaves `ip_str` null for IPv6-only services —
106,419 records, 1.19% of the source. `ipv6` is not projected, so these hosts
have a null `ip`.

Measured impact rather than assumed: **81,945 of them (77%) have no entity
anchor at all** and are already dropped by `int_entity_hosts`, so they cost
nothing. The remaining **24,383 attribute to 3,570 entities** — 1.6% of
228,570 — and contribute every signal normally, because entity resolution keys
on hostnames rather than addresses.

The residual cost is that `count(distinct ip)` would drop those hosts and
understate the estate for those 3,570 entities, which feeds the ICP size band.
Mitigated by falling back to the first hostname in the distinct count. The app
still cannot display an address for them.

A fifth re-ingest to capture `ipv6` was considered and rejected: 40 minutes of
wall clock to recover a display field for 1.6% of entities, against a fixed
deadline.

Found by a `not_null` test that was itself wrong — it asserted a property the
source does not have. Replaced with `assert_hosts_are_identifiable`, which
tests the weaker claim that actually matters: every host must have an address
*or* a name, or it can be neither attributed nor shown to a rep.

**[gap] ~26% of records have no resolvable domain.**
They are excluded from entity grouping rather than attributed by IP or ASN.
Returning `NULL` instead of inventing a fallback is deliberate — a wrong
entity anchor is worse than a missing one — but it does mean a
quarter of the raw data never reaches the prospect list.

---

## Vulnerability data

**[risk] Every CVE finding is version-inferred, never confirmed.**
Shodan attaches CVEs by matching the detected product version against known
advisories — it does not test the host. Measured across 7,066 CVE entries in a
5,690-record sample, `verified` is `false` for **100%** of them. So a host
reporting 99 CVEs on Apache 2.4.41 may have every one of them backported and
patched; the banner cannot tell us.

Consequence: raw CVE counts are close to meaningless as a ranking signal, and
any outreach that leads with "you have 99 vulnerabilities" is likely to be
wrong and will lose the rep credibility on the first call.

Mitigation: scoring leans on **EPSS** (exploitation probability in the next 30
days) rather than CVE count or CVSS. EPSS is an externally published score
about the vulnerability, not an inference about this host, so it survives the
same uncertainty. Severity language in generated briefs is hedged to what the
banner actually supports — the product and version are observed facts, the
vulnerability is a possibility.

**[gap] CVSS alone would have produced a misleading ranking.**
Two sampled hosts both report max CVSS 9.8. One has a bug with EPSS 0.99999,
the other 0.01225 — identical severity, completely different urgency. A
CVSS-ordered list would have treated them as equivalent.

---

## Transformation (dbt)

**[gap] Scoring weights are hand-tuned, not fitted.**
Every component weight in `company_scores` is a judgement call. There is no
conversion data to fit against — no record of which accounts actually bought —
so the weights encode a plausible theory of urgency, not a measured one. The
components are kept as separate columns specifically so they can be re-weighted
against real outcomes once any exist, and so a rep can disagree with a ranking
by looking at what drove it.

**[risk] Host count is a weak size proxy.**
It is the only size signal available, and it distorts in both directions: a
real company on shared hosting exposes one host and looks tiny, while a company
using many subdomains looks large. The ICP size bands inherit that error
directly. Firmographic data (headcount, revenue) would replace it.

**[fixed] IP addresses were becoming entities.**
Certificates can be issued to a bare IP address, and the certificate-CN
fallback accepted them, so addresses with no organisation behind them entered
the prospect pipeline. Found by eye, reading a 25-row sample built for the eval
set — `155.159.120.87` appeared as a candidate company.

Nothing caught it because nothing could: the value is a valid, non-null,
correctly-typed string, so every schema test passed. Fixed by rejecting
IPv4 and IPv6 forms in staging, and guarded going forward by
`assert_entities_are_not_addresses`.

**[risk] Certificate-derived entity keys can attribute to the wrong company.**
`int_entity_hosts` falls back to the certificate CN when there is no reverse
DNS record. A certificate can legitimately be issued for a domain hosted
elsewhere, so a cert-anchored host may belong to a different company than the
one the domain names. `entity_source` is carried through the whole pipeline so
these can be identified, filtered, or weighted down — but they are currently
treated the same as hostname-anchored records.

**[fixed] Tiering treated "unclassified" as "is a prospect".**
The first build put hosting providers at the top of tier A — Beget, Forpsi,
startdedicated, vps-10, hostsila, plus the reverse-DNS zone `64.in-addr.arpa`.
All scored 90 fit and 100 intent, because a multi-tenant estate accumulates
every finding belonging to everyone it hosts.

The scores were correct; the inference from them was not. Rules can prove an
entity **is** infrastructure — a seed match, a cloud tag, a reverse-DNS zone —
but they cannot prove it **is not**. Absence of evidence was being read as
evidence of absence, so anything the seed list had never heard of was promoted
to a call-now prospect.

Fixed by gating tiers on classification status: `unresolved` entities now land
in `U - unclassified` and cannot reach A–D until the LLM adjudicates. That
asymmetry is the clearest justification for the rule-vs-LLM split in the whole
system, and it was found by reading twenty rows rather than by reasoning about
the design.

**[gap] The seed list will never catch the long tail, by construction.**
Regional hosting providers are the single largest source of false prospects and
there are thousands of them. Adding the ones visible in a 20-row sample would
overfit to that sample without generalising, so two shape-based heuristics were
added instead — sequential machine naming (`srv12.`, `vps-104.`) and port
diversity disproportionate to estate size. Both are deliberately weak; the LLM
tier absorbs what they miss.

**[trade-off] The infrastructure volume heuristic thresholds are arbitrary.**
`cloud_host_ratio >= 0.9 and n_hosts >= 50` catches providers absent from the
seed list. Both numbers were chosen by inspection, not tuned. Too loose and
real multi-cloud companies get classified as infrastructure; too tight and
providers leak into the prospect list. The LLM tier exists partly to absorb
this imprecision.

**[gap] The seed provider list is manually curated and will go stale.**
~70 patterns covering the providers visible in this snapshot. New hosting
companies appear constantly, and nothing refreshes the list. In production this
would be derived from ASN ownership data rather than string matching.

**[risk] WAF posture is classified by regex over vendor strings.**
Distinguishing an incumbent security vendor from a CDN-bundled WAF drives the
whitespace score, and it is done with a pattern match over Shodan's vendor
label. A new vendor name, or a rename, silently falls through to `cdn_basic`
and overstates the opportunity.

**[gap] No cross-domain company resolution.**
`acme.com` and `acme.co.uk` are treated as two companies. Merging them needs
either firmographic data or an embedding-based match that is out of scope here,
so multinational estates are fragmented across several entities.

**[trade-off] `primary_country` uses the modal value.**
For a genuinely multinational estate the mode is close to arbitrary. The full
country list is retained alongside it so territory filtering can use either.

---

## LLM layer

**[fixed in v4] Prompt caching now engages on Haiku 4.5.**
v4's cacheable prefix measures 4,580 against the 4,096 floor and caches —
observed as a write on a cold call and reads on the two following it, with
input dropping from 4,492 to 513. Projected cost falls from $0.00518 to roughly
$0.00155 per call.

Whether v4 should actually ship depends on the eval, not the cost: it is a
different prompt, and its accuracy is measured against the held-out labelling
batch rather than assumed. The caching gain is a reason to *consider* v4, not a
reason to adopt it.

**[historical] Four attempts to diagnose this all failed the same way.**
The account below is kept because the failure mode generalises well beyond
caching.

The floor applies to the cacheable prefix — tools plus system, everything
before the breakpoint — not to total input, and not to anything computable from
character counts. Every attempt derived the prefix instead of observing it:
first from chars-per-token, then from subtracting an estimated message size
from an observed total, then from a purpose-built measuring script that
subtracted a token count of the message from a token count of the request. That
last one over-reported by ~315 tokens, enough to report v3's prefix as 86
*above* a floor it was ~230 *below*.

Each estimate was within about 5% and each landed on the wrong side of a hard
cutoff. `cache_creation_input_tokens` on a cold call reports the block exactly
and costs a fraction of a cent. See `docs/DECISIONS.md` D12e.

**[superseded] Prompt caching does not engage on the shipped prompt.**
`cache_control` is placed correctly — the identical code caches on Sonnet 5 —
but the v3 cacheable prefix on Haiku 4.5 measures around **4,062 tokens against
a 4,096 minimum**. Roughly 34 tokens short.

The floor applies to the *cacheable prefix* (tools + system, everything before
the breakpoint), not to total input. The user message sits after the breakpoint
and never forms part of the cached block, so reading the floor against total
input overstates the prefix by the length of the message.

Three attempts to clear the threshold failed because each reasoned from a
chars-per-token estimate rather than a measurement. The estimate ran ~5% high,
which is harmless against a gradient and fatal against a hard cutoff. Settled
by `llm/probe_cache_floor.py`, which sweeps prefix sizes and reads the true
value from `cache_creation_input_tokens` on a cold call: refused at ~3,875,
cached at 4,203. Forced and automatic `tool_choice` behaved identically, so
that was never implicated.

**Not fixed in v3 on purpose.** Adding ~300 tokens clears the floor and cuts
cost from $0.00518 to roughly $0.00155 per call. That is a prompt change, and
the eval measured v3 as it currently stands — shipping an edited prompt while
quoting the unedited prompt's precision would invalidate the measurement.
Costed and deferred to v4. See `docs/DECISIONS.md` D12d.

**[fixed] Cost estimator understated by 5.7×.**
It omitted the tool schema (sent on every request), used 4 chars/token against
markdown that tokenises nearer 3.2, and assumed 70 output tokens against a
measured 288. Corrected against real traces and now reports whether the
cacheable block clears the model's floor.

**[trade-off] Output verbosity is a deliberate cost.**
Output is billed at 5× input, and the `reasoning` field was the largest single
line in the bill before being constrained to 20 words. Keeping it at all costs
roughly 35% more than returning a bare label — paid for auditability, since it
is what makes a classification reviewable against the labelled set.

**[gap] Only part of the queue is classified.**
43,577 entities qualify; the budget covers a fraction. The queue is ordered by
fit so a partial run covers the entities most likely to be real companies, but
everything below the cut stays `U - unclassified` and never reaches a rep.
Production would run the full queue on a batch endpoint overnight.

**[measured] Classification precision is 0.750 on the class that matters.**
v3 on Haiku 4.5, against 25 hand-labelled entities: accuracy 0.520, precision
on `end_customer_company` 0.750, recall 0.500. One false positive
(`ibercsm.net`) would have reached a rep's call list. Full analysis in
`evals/RESULTS.md`.

**[risk] The eval set is too small to separate the configurations.**
`end_customer_company` has support of 6 and each configuration made four
predictions in it, so the difference between 0.750 and 0.500 precision is one
row. Only v3-over-v2 is defensible, because it moved three metrics at once on a
fixed model. Everything else is noise, and the numbers should not be read to
three decimal places. 100–150 examples with two labellers and adjudicated
disagreements is what these comparisons need.

**[risk] The labeller had evidence the model did not.**
Single-host entities with unfamiliar names — `gane.com.br`, `provet.in`,
`bml.cz`, `web.com`, `xssl.net` — fail identically across every configuration.
The human resolved them by looking the companies up; the model sees one host, a
product string, and an org name belonging to whoever owns the IP block. The
scores therefore understate the model relative to its inputs, and simultaneously
identify a real capability gap in the product. Both readings are true. The fix
is more evidence — HTTP page titles, WHOIS, or a search tool — not prompt
tuning.

**[gap] A known prompt fix is deliberately unapplied.**
`ax5z.com` carries `myra security` in its org list and every configuration
missed it. Unlike the single-host cases the evidence was present and unused, and
an instruction to scan the org list for security-vendor names would likely fix
it. Not applied: changing a prompt after seeing an eval and then reporting that
same eval turns a measurement into a fiction. It belongs in v4, against a set
built after the change.

**[risk] The stronger model scored worse on the deployment metric.**
Sonnet 5 had the best accuracy (0.600) and the worst precision on
`end_customer_company` (0.500), because it commits where Haiku hedges.
Decisiveness is the wrong disposition when a wrong commitment reaches a
salesperson. Worth flagging because the intuitive move — upgrade the model — is
the wrong one for this metric.

**[gap] No human-review queue is wired up.**
Low-confidence classifications are recorded with their confidence score but
there is no interface for a human to adjudicate them. The data supports it; the
workflow does not exist.

---

## Evals

**[risk] One labeller, no adjudication.**
All 25 labels come from a single person with no second opinion and no measure
of inter-rater agreement. Where a label is wrong, the model is penalised for
being right, and nothing in the process would surface that. Two labellers with
disagreements adjudicated is the standard fix and was skipped for time.

**[risk] The labeller had evidence the model did not.**
Labels were resolved partly by looking companies up. The model sees one host, a
product string and an org name belonging to whoever owns the IP block. Every
score therefore understates the model relative to its actual inputs — and
simultaneously identifies a real capability gap in the product, since the
product does need to identify those companies. Both readings are true; which
one you act on decides whether you tune the prompt or go and find more
evidence.

**[gap] No inter-version significance testing.**
`run_eval.py` reports deltas between configurations but no confidence interval.
With support of 6 on the headline class, a one-row difference moves precision
by 0.25, so the deltas invite over-reading. The report states this in prose;
the harness should state it in arithmetic.

**[gap] Prompt worked-examples are excluded, but nothing else is.**
`build_labelled_set.py` parses the prompt files and excludes any entity used as
a worked example, so the obvious leakage path is closed. Entities the prompt
describes *generically* — a named provider brand, a recognisable ccTLD pattern
— are not excluded and cannot easily be.

**[trade-off] The eval measures classification only.**
Scoring, tiering and outreach generation have no labelled ground truth. They
are deterministic and covered by dbt tests, so they are verifiable rather than
measurable — but "the tiering is sensible" is currently an assertion backed by
reading output, not a number.

---

## Application

**[risk] Tier A is too large to be a call list.**
Of 3,000 classified entities, 971 were confirmed organisations and **787 of
those — 81% — landed in tier A**. The cause is structural rather than a bad
threshold: the model queue is already filtered to entities carrying at least
one security signal, so by the time scoring runs, intent is high for almost
everything that survives. `intent_score >= 50` no longer discriminates.

The app still ranks correctly within the tier, so the list is usable top-down,
but the tier label has stopped carrying information. The fix is to set the
threshold from the distribution after filtering rather than from a round
number chosen before it — or to make tier a percentile rather than an absolute
cut. Not applied, because retuning thresholds to produce a pleasing tier
distribution after seeing the output is the same error as tuning a prompt after
seeing its eval.

**[trade-off] The ranking skews heavily public sector.**
Nine of the top fifteen tier-A accounts are universities, research institutes
or government bodies. This is a genuine property of the data rather than a
defect: public-sector estates are large, old and heterogeneous, so they
accumulate findings, and their domains make them easy to identify confidently.

They are real buyers, but the sales motion is different — procurement and
tenders, not a cold call. Surfaced rather than suppressed, via a segment filter
defaulting to commercial-only, so a rep works one list or the other rather than
a mixed one.

**[fixed] The opener paired a product with a CVE that did not belong to it.**
`headline_cve_product` and `headline_cve` are both aggregated per entity by
`arg_max(..., top_cve_epss)` — but from potentially *different services on the
same host*, since the aggregation picks each column independently. The draft
outreach therefore asserted things like "you are running Apache httpd, a
version associated with CVE-2015-0235" — CVE-2015-0235 is GHOST, a glibc
vulnerability, not an Apache one.

Caught by reading three generated openers aloud before recording a demo. A
wrong technical claim in a first email is worse than a vaguer true one: it is
the single fastest way for a rep to lose credibility, and it would have been
invisible in any test that checked the text was non-empty.

The opener now cites the CVE and its exploitation probability, states plainly
that it is inferred from a version banner rather than tested, and asks the
recipient to confirm rather than diagnosing for them. The proper fix is to
carry product and CVE through the aggregation as a single struct so they cannot
be separated — deferred, and the reason recorded here.

**[gap] Outreach openers are template-generated, not model-written.**
The "what to say" text is assembled deterministically from the specific finding
rather than generated. It is accurate and it hedges correctly on
version-inferred CVEs, but it will read the same across accounts sharing a
finding type. An LLM-drafted opener per account is the obvious next increment
and was scoped out for time.

**[trade-off] The app reads a static snapshot.**
No database, no refresh, no auth. Correct for a demonstration and for the
batch-enrichment architecture, but a production deployment would need a
scheduled rebuild of the serving artifact and access control over it.
