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

## The bugs that mattered, and how they were found

None was found by testing.

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

**A generated email asserted a false technical claim.** "Apache httpd ...
associated with CVE-2015-0235" — GHOST is a glibc bug, not an Apache one. The
cause was two independent `arg_max` aggregations: the product with the highest
EPSS and the CVE with the highest EPSS, picked separately and then printed in
one sentence as though related. Every value was correct; the sentence was not.
Fixed by not asserting the product alongside the CVE at all.

**And a fourth, found the same way, after the app was already live.** Rendering
720 drafts to check the new tone controls surfaced EPSS of 0.9997 printing as
"100% chance of exploitation" — a certainty claim in a cold email, undoing the
hedging the rest of the copy is built around.

The pattern is the same every time. Schema tests verify that data has the right
shape. They cannot tell you the shape is describing the wrong thing — and three
of these four were only visible by reading the thing a human would actually
receive.

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

**I did not retune the tier thresholds after seeing that 779 of 972 prospects
landed in tier A.** That number is too high to be a call list, and the cause is
understood — the queue is pre-filtered to entities with a finding, so the
urgency threshold stopped discriminating. But adjusting a threshold after
seeing the distribution, to produce a nicer-looking split, is the same error as
tuning a prompt after reading its eval. It is documented instead.

**I did not apply a known prompt fix — until there was a clean set to measure
it against.** `ax5z.com` has `myra security` in its organisation list and every
configuration missed it. An instruction to scan that field for vendor names
would likely fix it, but applying it and then reporting the same eval would
turn a measurement into a fiction.

So it waited. When I tripled the labelled set, the objection evaporated: v4
carries the fix, and it is scored on a batch built after the change, reported
separately from the batch its wording was derived from. Held-out precision went
0.385 to 0.467 and recall 0.444 to 0.611. The discipline cost nothing in the
end — it just moved the work to the point where the number meant something.

**I stopped investigating the caching failure, and then restarted it for the
wrong reason.** I wrote it up as economically irrelevant — about $4 at the
volume being run — and moved on. That was defensible arithmetic and the wrong
call, because the cost of *not* knowing was not $4. It was that I could not say
whether the shipped configuration cached, and the answer turned out to be worth
2.5× per call.

What settled it was measuring instead of reasoning: one real API call, reading
`cache_creation_input_tokens` back off the response. Four prior attempts had all
inferred the prefix length from a chars-per-token estimate that ran about 5%
high — invisible against a gradient, decisive against a hard cutoff. Three
rounds of argument that a two-minute probe ended.
