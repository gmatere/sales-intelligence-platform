# Build journal

Kept during the build rather than reconstructed afterwards, because the honest
version of "where did AI help and where did it cost me" is only available while
it is happening. Source material for the How You Build reflection.

Tooling: Claude Code as the primary loop — it wrote most of the Python and SQL
from specifications I gave it, and I reviewed, tested and corrected. All heavy
processing ran on a dedicated cloud instance; my laptop only ever held source
code and the final curated output.

---

## Day 1 — Tue 15 Sep

**Biggest single win, and it was reconnaissance rather than code.** Before
writing anything I had the first 8 MB of the compressed dump pulled and
profiled. It turned out not to be company data at all — it is a Shodan
internet-scan dump, one record per exposed service. I had been planning around
firmographics. That profiling step cost ten minutes and killed a day's worth of
wrong assumptions.

Lesson I then failed to apply: I profiled enough to identify the dataset, not
enough to design the projection. That cost me three restarts on Day 4.

**Infrastructure friction, unrelated to AI.** Lost most of an evening to AWS
India KYC on a personal account — an Aadhaar name mismatch that could not be
resolved from the console. Abandoned it for a DigitalOcean droplet, which took
ten minutes. Worth recording only as a reminder that the obvious provider is
not always the fast one.

---

## Day 4 — Fri 18 Sep

**Three ingest restarts, all from the same root cause.** The first projection
carried 30 columns. Reviewing the raw schema properly afterwards surfaced three
things it had missed:

- `http.waf` — present on 8.6% of records, and the values are vendor names
  (Fortinet, Imperva, Cloudflare). This directly answers "do they already have
  a security vendor", which is the strongest commercial signal in the dataset.
- `http.components` — the detected technology stack.
- The `vulns` entries carry `cvss`, `epss` and `verified`, not just CVE IDs. I
  had projected only the IDs, which would have left the scoring model counting
  vulnerabilities — the exact naive approach I had been arguing against.

Each miss cost a 40-minute re-ingest. A proper path-by-path audit of the source
schema up front would have caught all three in one pass; I eventually wrote
that audit, and it found nothing further, which is the point.

**Where the generated code was wrong, and how I found out.** Reading the output
directory mid-run raised `No magic bytes found at end of file
part-00006.parquet`. Parquet writes its footer last, so that truncated file is
exactly what a crash would leave behind — and the resume logic counted shard
files by glob, so it would have treated it as complete and skipped 100,000
records. Fixed with staged writes and an atomic rename.

The generated documentation had asserted the opposite: that partial shards are
never written. The code and its own comments disagreed, and only running it
surfaced which was right.

---

## Day 5 — Sat 19 Sep

**Two tests failed and both were my fault, in different ways.**

`not_null` on the IP column failed with 106,419 rows. The assertion was wrong,
not the data: IPv6-only hosts populate `ipv6` and leave `ip_str` null. I had
written a test encoding an assumption I never verified. Replaced it with the
weaker, true claim — every host must have an address *or* a name — and measured
the impact before deciding whether to re-ingest. 77% of those records had no
entity anchor at all and were already being dropped; the rest affected 1.6% of
entities. Not worth 40 minutes against a deadline.

A second test then failed at 81,945 rows. That one was *correct* about the data
and wrong about its layer — staging is a faithful projection, so unusable
records legitimately live there. Moved to a warning with an error threshold
above the known baseline, so the count stays visible and a regression still
breaks the build.

**The most valuable thing I did all day took two minutes.** I queried the top 20
tier-A accounts. Eighteen were hosting providers and one was a reverse-DNS
zone, all scoring 90 fit and 100 intent.

Nothing was broken. The scoring was right, the tests passed, the SQL did what it
said. The flaw was conceptual: the tiering logic treated "rules found no
evidence of infrastructure" as "confirmed company," when rules can only ever
prove the positive. That was invisible in the code and obvious in twenty rows of
output.

This is the clearest thing I would tell a teammate: the generated code was
correct and the design was wrong, and no amount of reading the code would have
shown it.

**Where AI cost me more than doing it by hand.** I asked for a static checker to
find missing column references across the dbt models before rerunning. It
produced enough false positives — string literals, CTE names, columns defined
upstream — to be useless, and I would have found the one real bug faster by
just running `dbt build`. Small, but a fair example of reaching for automation
where the feedback loop was already fast enough.

**The cost estimate was wrong by 5.7× and the traces are the only reason I
know.** I wrote a dry-run estimator so the token budget would be a decision
rather than a discovery, ran it, got $24 for the full queue, and felt good
about it. Then I ran 25 real calls and the traces said $0.0031 each — $136
extrapolated.

Three errors, all in the same direction. The estimator ignored the tool schema,
which is sent on every request. It used 4 chars per token against markdown that
tokenises nearer 3.2. And it assumed 70 output tokens where the real figure was
288, because I asked for a reasoning field without constraining its length.

The third error is the interesting one: **prompt caching was never working.**
Anthropic requires a minimum block length to cache — 2048 tokens for Haiku —
and mine was 1,118. The `cache_control` marker is silently ignored below that.
No error, no warning, no indication in the response beyond `cached_tokens: 0`
in a field I only had because I'd built the trace schema before I needed it.

That is the strongest argument I have for why per-call tracing is not optional
scaffolding. A silent pricing failure has no other surface.

**Resolved the caching thing, and the answer was that I was wrong, not the
docs.** I'd assumed three times over that the documented minimum cacheable
prefix didn't apply to us. It did. Our block was about 34 tokens under 4,096.

The error is worth naming precisely: the floor applies to the *cacheable
prefix*, not to total input, and I'd been measuring total. The user message
sits after the cache breakpoint and never forms part of the block, so reading
the floor against total input overstates the prefix by however long the message
happens to be.

Underneath that, a simpler failure: every attempt reasoned from a
chars-per-token estimate. None measured. The estimate ran about 5% high, which
is invisible against a gradient and decisive against a hard cutoff. A sweep
across prefix sizes settled it in two minutes and cost a few cents — three
rounds of argument that could have been one probe.

I'm not applying the fix. Adding 300 tokens would clear the floor and cut cost
3.3×, but the eval measured this prompt as it stands, and shipping an edited
prompt while quoting the old one's precision is the thing I've been careful
about everywhere else.

**Sampling beat reasoning again.** Two random samples of 25 excluded entities
each contained exactly one false positive, both government bodies, both scoring
0.62–0.64 on the hosting heuristic while every real ISP sat at 0.79–1.00. I
would not have picked that threshold by argument. Twenty-five rows, twice, cost
about five minutes.

**Applied the fix after all, once it could be measured honestly.** The entry
above says I wasn't going to add the 300 tokens, because the eval had scored
the prompt as it stood. That held right up until I grew the labelled set —
at which point there was a clean set to score a new prompt against, and the
objection disappeared. v4 is v3 plus margin over the 4,096 floor, scored on
data built after the change, with batch-1 and batch-2 reported separately
because v4's wording was written after reading v3's batch-1 failures.

It caches: 4,214 read tokens per call, $0.00504 down to $0.00205. A *longer*
prompt costing 2.5× less is the least intuitive result in this project.

**Then the eval reversed the whole recommendation.** At 25 labels v3-on-Haiku
led at 0.750 and I argued in writing against Sonnet at 0.500. At 75 labels
Haiku is last at 0.385 held-out and Sonnet is first at 0.667. Nothing changed
but the label count. The caveat I'd written at n=25 — support of 6, one row
worth 0.25 of precision, only one comparison defensible — turned out to be the
only line in that table worth anything.

Reversed the recommendation and re-ran production on Sonnet. Writing the
limitation down at the time is what made the reversal a two-line decision
instead of an argument with myself.

**Paid for the production run twice.** The first Sonnet run wrote results
before `--model` was being recorded, so 3,000 rows carry `model: null`. The
rerun's resume key is the `(prompt_version, model)` pair, which matched none of
them, so it classified all 3,000 again. 6,000 calls, $21.28, 3,000 verdicts.

The resume logic was correct and the data predated it. A key change is a
migration — rows written under the old key are invisible to the new one, and
"resume" quietly becomes "restart". Worth remembering the next time I tighten
an identity.

**I concluded the re-export changed nothing, and that was wrong.** The file I
compared was byte-identical to the committed one, so I reasoned that both
blocks were Sonnet, later-wins had already picked the same rows, and the
`(version, model)` filter had changed a guarantee rather than an output.

The first half was right and the conclusion was not. The processing box had
never pulled the export fix, so the command I thought had re-exported had in
fact not run the new code at all — the file on disk was still the *first* run's
output, which is why it matched. Once the fix was actually present, the same
command produced different verdicts on 136 of 3,000 entities and moved 96
tiers.

Byte-identical output is weak evidence that two code paths agree. It is much
stronger evidence that one of them did not run. I checked the artifact and
forgot to check that the thing producing it was the thing I had written.

**Which handed me a measurement I had been planning to ask for.** The two
blocks are independent runs of the same prompt on the same model over the same
3,000 entities, so the disagreement between them *is* run-to-run
self-consistency: 95.5% on class, 96 tier changes, mean confidence movement of
0.023.

That 4.5% flip rate is the floor under every eval number in this project. The
gap between the best and worst configuration at n=75 is a couple of rows out of
12 on the held-out batch — the same order as the noise a single model has
against itself. It does not make the ranking wrong, but it means "v3-Sonnet
beats v2-Haiku by 0.167" is a statement about one sample, not a property of the
models.

Two accidents in a row — the double-billed run and a stale checkout — produced
better evidence about eval reliability than the eval did.

**Read the top of tier A one more time before shipping.** Four of the top 20 —
`korbank.pl`, `lodz.pl`, `castle-it.net`, `e-pos.link` — are probably ISPs or
hosts, all at confidence 0.60–0.75. That is four in twenty against a measured
precision of 0.615, so the artifact is behaving exactly as measured rather than
better. Raising the confidence floor to 0.75 would clear three of them and I
didn't do it: picking a threshold after seeing which rows it removes is the
same error as tuning a prompt after seeing its eval.

Three of the four bugs I found in this project came from reading output —
hosting providers in tier A, an IP address as a company, a CVE attached to the
wrong product. None came from a test. Tests hold the shape; only reading tells
you the shape is wrong.
