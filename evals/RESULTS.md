# Eval results

Four configurations against 75 hand-labelled entities, sampled stratified
across estate size and deliberately over-weighted toward the boundary cases the
deterministic rules very nearly excluded.

Labels are human ground truth. The model never sees them; nothing is fed back.
Entities used as worked examples in any prompt are excluded automatically, by
parsing the prompt files rather than by remembering to.

---

## Headline

**Precision on `end_customer_company` is the metric that governs deployment.** A
false positive puts a hosting provider into a rep's call list; they phone a
datacenter about its customers' vulnerabilities and never trust the tool again.
A false negative removes a real company from the market — costly, but invisible
and recoverable.

| Config | Accuracy | Precision (all 75) | **Precision (held-out batch)** | Cost/call |
|---|---:|---:|---:|---:|
| **v3 · Sonnet 5** | 0.667 | 0.615 | **0.667** | $0.00378 |
| v2 · Haiku 4.5 | 0.653 | 0.588 | 0.500 | $0.00341 |
| v4 · Haiku 4.5 | 0.667 | 0.524 | 0.467 | ~$0.00155 |
| v3 · Haiku 4.5 | 0.627 | 0.471 | 0.385 | $0.00518 |

**Shipped: v3 on Sonnet 5.**

---

## The result that matters most is that the previous result was wrong

An earlier version of this document reported the same comparison at n=25 and
recommended **v3 on Haiku**, which measured 0.750 precision — the best of three
configurations. It argued explicitly against Sonnet, which measured 0.500, on
the grounds that Sonnet "commits where Haiku hedges" and that decisiveness is
the wrong disposition when a wrong commitment reaches a salesperson.

At n=75 that ordering **completely inverts**:

| Config | n=25 precision | n=75 held-out precision |
|---|---:|---:|
| v3 · Haiku | **0.750** — recommended | **0.385** — worst |
| v2 · Haiku | 0.667 | 0.500 |
| v3 · Sonnet | 0.500 — argued against | **0.667** — best |

Nothing changed but the number of labels. The n=25 table was noise presented to
three decimal places.

**The caveat was already in that document before the reversal.** It stated that
support of 6 meant one row moved precision by 0.25, that only v3-over-v2 was
defensible, and that 100–150 examples with two labellers was what the
comparisons actually needed. That caveat turned out to be the only reliable
line in the table — which is the argument for writing the limitation down at
the time rather than after being caught out by it.

It is also the argument for *acting* on it. The conclusion was reversed and the
production run redone on Sonnet, rather than leaving a recommendation the
evidence no longer supported.

---

## What holds up at n=75

### v4 beats v3 on Haiku, confirmed on held-out data

| | v3 · Haiku | v4 · Haiku |
|---|---:|---:|
| Held-out precision | 0.385 | **0.467** |
| Held-out accuracy | 0.660 | **0.700** |
| Recall | 0.444 | **0.611** |
| Cost/call | $0.00518 | **~$0.00155** |

v4 added an instruction to scan the organisation list for security-vendor
names, guidance that a recognisable organisation name outweighs a small estate,
and three worked examples. All three moved in the right direction on the batch
the prompt was **not** tuned against, which is the only measurement that counts
here — v4's changes were written after reading v3's failures on batch 1, so
v4's batch-1 score is contaminated by construction.

It also caches, cutting cost 3.3×. That was not enough to outweigh Sonnet's
precision advantage, but it makes v4 the right Haiku configuration if cost ever
becomes the binding constraint.

### Sonnet's advantage is concentrated where it matters

`isp_telco` precision of **1.000** against Haiku's 0.875–0.900, and the fewest
false positives on the headline class — 5, against 7 for v2, 9 for v3-Haiku and
10 for v4. The failure mode that ends a sales call is the one it makes least
often.

---

## What does not hold up, even now

**`unknown` is massively over-predicted.** Only 2 of 75 entities are genuinely
unknown, yet configurations return it between 36 and 87 times. Precision on
that class is 0.105–0.143. This is the single largest source of lost recall and
it is downstream of a prompt instruction — *prefer `unknown` over a confident
wrong exclusion* — working exactly as written, and arguably too well. Tuning
that trade-off is the highest-value prompt work remaining.

**Some labels are probably wrong.** `3cx.ae` is 3CX, a PBX *software* vendor,
labelled `isp_telco` here and called `end_customer_company` by three of four
configurations. `digitalags.net` is similar. Where a label is wrong the model is
penalised for being right, and every configuration is understated equally. One
labeller with no adjudication is the root cause.

**75 is better than 25 and still small.** Support on the headline class is 18
overall, 12 on the held-out batch. The gap between 0.667 and 0.500 is two rows.
The reversal documented above is exactly what a set this size can still do, and
the honest reading is that the current ordering is *more* trustworthy than the
previous one, not that it is settled.

---

## The systematic failure, unchanged across all four configurations

Single-host entities with unfamiliar names: `bml.cz`, `bond.style`,
`tizbi.com`, `tvssite.com`, `rajsyru.cz`. Every configuration returns `unknown`
or guesses wrong.

The cause is not the prompt. The labeller resolved these by looking the
companies up; the model receives one host, a product string and an organisation
name belonging to whoever owns the IP block. There is genuinely insufficient
evidence in the input to identify a small business.

**This is a confound in the eval as much as a finding about the model.** Two
honest readings:

- *The comparison is unfair.* `unknown` at confidence 0.30 on one host with no
  identifying evidence is correct behaviour given those inputs, and the prompt
  instructs exactly that.
- *The comparison is exactly right.* The product must identify companies. If
  the pipeline cannot, that is a real capability gap.

Both are true. The second determines what to build next: **more evidence, not
better prompting.** HTTP page titles, WHOIS registrant data or a search tool
would resolve most of these.

---

## Cost and latency, measured

| Config | Cost/call | p50 latency | Caching |
|---|---:|---:|---|
| v2 · Haiku | $0.00341 | 1,702 ms | no — prefix below the floor |
| v3 · Haiku | $0.00518 | 1,716 ms | no — prefix ~230 tokens short |
| v4 · Haiku | ~$0.00155 | 1,743 ms | **yes** — 4,580-token prefix |
| v3 · Sonnet | $0.00378 | 1,767 ms | **yes** — 5,541-token prefix |

Across 3,000 entities the spread is roughly $5 to $16. The configuration was
chosen on precision; the cost difference was not close to decisive.

---

## Reproducing

```bash
python evals/build_labelled_set.py 50      # new batch, labels blank
python evals/make_worksheet.py             # plain-text worksheet
python evals/merge_labels.py               # merge labels into the set
python evals/run_eval.py --prompt-version v3 --model claude-sonnet-5
```

`run_eval.py` reports against the previous stored result and keys every result
on the **(prompt version, model)** pair. Keying on version alone silently mixed
two models inside one version during development and produced a meaningless
average — the configuration is the pair, not either half. The same mistake
appeared independently in the classifier's resume logic and in the trace
analysis, and was fixed in all three places.

Per-batch scores are reported separately because a prompt revised after reading
failures on one batch is tuned on it. Later batches are the only clean read.
