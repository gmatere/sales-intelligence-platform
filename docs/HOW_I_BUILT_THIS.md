# How I built this

Claude Code as the primary loop, throughout. I specified, it implemented, I
reviewed and corrected. Heavy processing ran on a disposable cloud instance;
my laptop only ever held source code and the final curated output.

The honest summary: **AI wrote almost all of the code, and every bug that
mattered was found by looking at output rather than by reading it.**

---

## Where it saved the most time

**Reconnaissance, before any code.** The first thing I did was pull 8 MB of the
compressed dump and profile it. It turned out not to be company data at all —
Shodan internet-scan banners, one record per exposed service. I had been
planning around firmographics. Ten minutes of profiling killed a day of
building against the wrong shape, and it set the entire product direction: this
is intent data, not fit data.

**Volume of correct, boring code.** The streaming ingest, the dbt layer, the
trace schema, the eval harness. Roughly two thousand lines I would otherwise
have typed. None of it was conceptually hard; all of it takes hours.

**Working at the edge of what I knew.** Prompt caching, EPSS, Shodan's field
semantics. I could describe the shape I wanted and get a first implementation
to react to, which is much faster than reading documentation to the point of
confidence before writing anything.

---

## Where it cost more than doing it by hand

**A static analyser I should not have asked for.** Before a rebuild I wanted to
check for missing column references across the dbt models. The checker it
produced flagged string literals, CTE names and columns defined upstream —
enough false positives to be useless. Running `dbt build` would have found the
one real bug in thirty seconds. I reached for automation where the feedback
loop was already fast.

**Confident wrong answers about an API, three times running.** Prompt caching
silently failed on every call. I was told the minimum cacheable prefix was
2,048 tokens; it is 4,096 for that model. I grew the prompt to clear 2,048,
then to clear 4,096, and it still refused. Only when I stopped asking and read
the actual documentation did I get the real table — and only when I ran the
same code against a different model did I confirm the implementation had been
correct all along. Three rounds of plausible-sounding guidance, each producing
working code that silently did the wrong thing.

**Generated documentation that contradicted its own code.** The ingest script's
comments asserted that partial Parquet shards are never written. They are. A
truncated shard was sitting on disk while the comment said it could not exist.

---

## The two bugs that mattered, and how they were found

Neither was found by testing.

**Tier A filled with hosting providers.** I queried the top twenty accounts.
Eighteen were hosting companies and one was a reverse-DNS zone, every one
scoring 90 fit and 100 urgency. Nothing was broken — the SQL was correct, the
tests passed, the scores were right. The flaw was conceptual: the tiering logic
read "rules found no evidence of infrastructure" as "confirmed company". Rules
can only ever prove the positive. That was invisible in the code and obvious in
twenty rows of output.

**IP addresses had become companies.** `155.159.120.87` appeared as a prospect
in a sampled eval set. Certificates can be issued to a bare address and my
certificate fallback accepted them. No test caught it because no test could:
the value is a valid, non-null, correctly-typed string.

The pattern is the same both times. Schema tests verify that data has the right
shape. They cannot tell you the shape is describing the wrong thing.

---

## What I would tell a teammate picking this up

**The eval set is 75 examples and it already caught me out once.** At 25 it
ranked v3-on-Haiku first at 0.750 precision and I wrote a recommendation
against Sonnet on that basis. At 75 the ordering inverted completely — Haiku
last at 0.385 held-out, Sonnet first at 0.667. Nothing changed but the label
count. I reversed the recommendation and re-ran production on Sonnet.

The current numbers — 0.615 precision on `end_customer_company`, 0.667
held-out — tell you the system works and are more trustworthy than the
previous set. They are not settled: support is 18 overall and 12 on the
held-out batch, so the gap between the top two configurations is a couple of
rows.

If you change one thing before shipping this further, make it the eval set:
150+ examples, two labellers, disagreements adjudicated. I would say that even
though I already tripled it once, because tripling it is precisely what showed
me the first answer was wrong.

**And a confound in my own methodology.** When labelling, I looked the companies
up. The model gets one host, a product string and an org name belonging to
whoever owns the IP block. Where it returned `unknown` on a single-host entity,
that may well be correct behaviour given its inputs — the prompt explicitly
instructs it to prefer `unknown` over a confident wrong answer. The scores
understate the model relative to what it was given, and simultaneously identify
a real capability gap in the product. Both are true, and which one you act on
determines whether you tune the prompt or go and find more evidence.

I would go and find more evidence.

---

## Things I chose not to do, and why

**I did not retune the tier thresholds after seeing that 787 of 971 prospects
landed in tier A.** That number is too high to be a call list, and the cause is
understood — the queue is pre-filtered to entities with a finding, so the
urgency threshold stopped discriminating. But adjusting a threshold after
seeing the distribution, to produce a nicer-looking split, is the same error as
tuning a prompt after reading its eval. It is documented instead.

**I did not apply a known prompt fix.** `ax5z.com` has `myra security` in its
organisation list and every configuration missed it. An instruction to scan
that field for vendor names would likely fix it. Applying it and then reporting
the same eval would turn a measurement into a fiction. It belongs in v4,
against a set built afterwards.

**I stopped investigating the caching failure once it stopped mattering.** The
whole question was worth about $4 on the volume actually being run. I wrote the
diagnostic script, recorded what was known, and moved on. The method —
instrument, measure, isolate with a minimal reproduction, stop when the answer
is economically irrelevant — is the part worth keeping.
