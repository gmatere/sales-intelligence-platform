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

**Sampling beat reasoning again.** Two random samples of 25 excluded entities
each contained exactly one false positive, both government bodies, both scoring
0.62–0.64 on the hosting heuristic while every real ISP sat at 0.79–1.00. I
would not have picked that threshold by argument. Twenty-five rows, twice, cost
about five minutes.
