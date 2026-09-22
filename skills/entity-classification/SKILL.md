---
name: entity-classification
description: >
  Classify internet-exposed estates as real companies or as infrastructure
  (hosting, CDN, ISP), so a B2B prospect list contains businesses rather than
  cloud providers. Use this skill whenever working with the sales intelligence
  pipeline — running or resuming a classification batch, adding or changing a
  prompt version, estimating what a classification run will cost before
  spending, evaluating classifier quality against the labelled set, or
  investigating why a particular entity was tiered the way it was. Also reach
  for it whenever a hosting provider, ISP or reverse-DNS zone turns up in a
  prospect list, whenever someone asks whether the classifier is any good, or
  whenever a decision needs making about which model or prompt version to run —
  those questions all have measured answers here rather than guesses.
version: 1.1.0
prompt: prompts/v3/entity_classification.md
model: claude-sonnet-5
measured:
  eval_set: evals/labelled_set.jsonl (75 hand-labelled entities, two batches)
  precision_end_customer_company: 0.615
  precision_held_out_batch: 0.667
  recall_end_customer_company: 0.444
  accuracy: 0.667
  results: evals/RESULTS.md
cost:
  per_call_usd: 0.00378
  ceiling_usd_per_refresh: 150
---

# Entity classification

Given every internet-exposed service found under one registrable domain, decide
what kind of organisation owns it.

This is the only decision in the pipeline that deterministic rules cannot make,
and the only place a model is called at volume. Everything else — CVE severity,
port exposure, certificate expiry, scoring, tiering — is settled in SQL, because
a rule is faster, cheaper and auditable where structure determines the answer.

## Decide whether to run at all

Run when:

- New entities sit in `llm_classification_queue` unclassified by the current
  prompt version
- The prompt version changed and existing entities need re-adjudicating
- An entity previously marked `unknown` has gained evidence worth a second look

Do **not** run when:

- A rule already settled it. Seed-list matches, reverse-DNS zones and the volume
  heuristics are final; a model call adds cost and no information.
- The entity has no security findings (`n_signal_categories = 0`). It is not a
  prospect whether or not it is a real company, so its classification is
  worthless. This filter removes more candidates than the denylist does — 83,464
  against 125,037 — and it is a `WHERE` clause rather than a model call.
- You are about to report an eval the prompt has already seen. Changing a prompt
  after reading its results and then quoting those same results turns a
  measurement into a fiction.

## Run it

**Price the work before spending.** No API calls; also reports whether prompt
caching will engage for the chosen model, which is worth knowing because it
fails silently when it doesn't.

```bash
python llm/classify.py --dry-run --prompt-version v3
```

**Classify.** The queue is ordered by ICP fit, so a partial budget still covers
the entities most likely to be real companies.

```bash
python llm/classify.py --limit 3000 --prompt-version v3
```

Results are keyed on (entity, prompt version). A killed run resumes; a prompt
change re-adjudicates rather than skipping.

**Verify before trusting the output.**

```bash
python evals/run_eval.py --prompt-version v3 --model claude-haiku-4-5
```

Reports per-class precision and recall against the previously stored result, and
prints every disagreement with the model's reasoning beside the human label — so
a failure can be attributed to the model or to the label.

**Publish** by merging verdicts into the serving artifact:

```bash
python export_curated.py
```

## Inputs

One row per entity from `llm_classification_queue`:

| Field | Meaning |
|---|---|
| `entity_domain` | registrable domain, the entity key |
| `orgs_seen` | registered orgs across the estate's IP blocks, normalised |
| `primary_country` | modal country of the estate's hosts |
| `n_hosts`, `n_ports`, `n_products` | estate shape |
| `products`, `technologies` | detected software |
| `waf_vendors` | detected web application firewalls |
| `cloud_host_ratio` | share of hosts in cloud or CDN address space |

Truncate list fields to twelve items before rendering. An entity with two
hundred technologies otherwise costs ten times one with five, for no extra
signal.

One row, as it reaches the prompt:

```json
{
  "entity_domain": "acme-industrial.com",
  "orgs_seen": ["amazon technologies inc"],
  "primary_country": "DE",
  "n_hosts": 6,
  "n_ports": 4,
  "n_products": 5,
  "products": ["nginx", "OpenSSH", "Postfix"],
  "technologies": ["WordPress", "Google Analytics"],
  "waf_vendors": [],
  "cloud_host_ratio": 1.0
}
```

## Outputs

A tool call against a fixed schema, never free text. The verdict for the row
above:

```json
{
  "entity_class": "end_customer_company",
  "canonical_name": "Acme Industrial",
  "confidence": 0.88,
  "reasoning": "Ordinary business name, coherent single-tenant stack, org names AWS only."
}
```

Note what carried the decision: `cloud_host_ratio` is 1.0 and the only org is
Amazon, yet this is a company. Hosting on AWS makes an entity Amazon's
*customer*, not Amazon — the estate's shape and the domain name decide it. That
distinction is the whole reason this step exists.

One of `end_customer_company`, `hosting_or_cloud`, `cdn_or_security_vendor`,
`isp_telco`, `government_or_education`, `unknown`.

Route anything below **0.60 confidence** to `R - needs review`. It must not
reach a rep without a human looking first.

Every call appends one line to `data/traces/entity_classification.jsonl` with
prompt version, model, tokens, cache usage, cost, latency, decision and
confidence. Keep that schema intact — it is what surfaced a prompt-cache failure
that raised no error of any kind.

## The judgement this encodes

Every threshold here is set by one asymmetry, and understanding it is more
useful than memorising the numbers.

A provider misclassified as a company enters a rep's call list. They phone a
datacenter about its customers' vulnerabilities, the call goes badly, and the
tool loses their trust permanently. **Precision on `end_customer_company`
governs deployment** — not accuracy.

A company misclassified as infrastructure disappears from the addressable market
invisibly, because nobody audits an excluded list. So where evidence does not
settle it, prefer `unknown` over a confident wrong exclusion.

Two consequences follow, and both look wrong until you apply the asymmetry:

**Run Sonnet, not Haiku — but check the reasoning before trusting it.** At 25
labels this said the opposite, and said it confidently: Haiku measured 0.750
precision against Sonnet's 0.500, and the conclusion drawn was that Haiku's
tendency to hedge suited the asymmetry better. At 75 labels the ordering
inverts — Haiku 0.385 on held-out data, Sonnet 0.667. Sonnet also makes the
fewest false positives on the headline class, which is the error that ends a
sales call.

The lesson transfers past this decision: a small eval will produce a ranking,
and it will look like a finding.

**Prefer an extra model call to a silent exclusion.** When tuning any rule
upstream of this skill, set thresholds so borderline cases fall through to
classification. An excluded company is never reviewed; an extra call costs a
fraction of a cent. That reasoning is why the hosting heuristic sits at 0.8
rather than 0.6 — two government bodies were being silently dropped at 0.64.

## When results look wrong

**A hosting provider reached a rep's list.** Check `rule_evidence` first — if
null, the rules never saw it and the model got it wrong. Add it to
`dbt/seeds/infrastructure_providers.csv` only if it is a *named* provider;
otherwise this is the long tail the model exists to handle, and the fix is
prompt work measured against the eval set.

**Everything is coming back `unknown`.** Expected on single-host entities with
unfamiliar names — there is genuinely insufficient evidence in a scan record to
identify a small business. This is not a prompt problem. It needs more evidence:
HTTP page titles, WHOIS, or a search tool.

**Costs are higher than the dry run predicted.** Check `cached_tokens` in the
traces. Caching fails silently below a model-specific minimum prefix length,
which is non-monotonic across generations — Haiku 4.5 requires 4,096 tokens
where Opus 5 requires 512. `llm/probe_cache_floor.py` finds the real threshold
empirically.

## Known weaknesses

**Single-host entities with unfamiliar names fail consistently.** `provet.in` is
a veterinary business; the model sees one host, nginx, and an org name belonging
to whoever owns the IP block. An evidence problem, not a prompting one.

**The eval set is small enough to have already misled once.** 75 examples,
support of 18 overall and 12 held out. At 25 it produced a ranking that
inverted completely on more data. The current ordering is better supported, not
settled — two rows still separate first from third.

**A known prompt fix is deliberately unapplied.** `ax5z.com` carries
`myra security` in its org list and every configuration missed it. An
instruction to scan that field for vendor names would likely fix it, but
applying it and re-reporting the same eval would invalidate the measurement.
It belongs in v4, against a set built afterwards.

## Dependent files

| Path | Role |
|---|---|
| `prompts/v3/entity_classification.md` | the prompt; v1 and v2 retained for comparison |
| `llm/classify.py` | runner — retries, concurrency, resume, dry-run estimator |
| `llm/tracing.py` | trace schema, pricing, per-model cache thresholds |
| `llm/probe_cache_floor.py` | empirical cache-threshold finder |
| `evals/run_eval.py` | scores a configuration against the labelled set |
| `evals/labelled_set.jsonl` | 75 hand-labelled entities, batched — human ground truth |
| `export_curated.py` | merges verdicts into the serving artifact |
