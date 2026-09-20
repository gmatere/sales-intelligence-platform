# Eval results

Three configurations against one hand-labelled set of 25 entities, sampled
stratified across estate size and deliberately over-weighted toward the
boundary cases the deterministic rules very nearly excluded.

Labels are human ground truth. The model never sees them; nothing is fed back.

Worked examples named in any prompt version are excluded from the set
automatically, by parsing the prompt files rather than by remembering to.

---

## Headline

| Config | Accuracy | **Precision, `end_customer_company`** | Recall | False positives |
|---|---:|---:|---:|---|
| v2 · Haiku 4.5 | 0.480 | 0.667 | 0.333 | `magma.ca` |
| **v3 · Haiku 4.5** | 0.520 | **0.750** | **0.500** | `ibercsm.net` |
| v3 · Sonnet 5 | **0.600** | 0.500 | 0.333 | `ibercsm.net`, `magma.ca` |

**Shipped: v3 on Haiku 4.5.**

Precision on `end_customer_company` is the metric that decides deployment. A
false positive there puts a hosting provider or an ISP into a sales rep's call
list; they phone a datacenter to discuss its customers' vulnerabilities, and
the tool loses their trust permanently. A false negative removes a real company
from the addressable market, which is costly but invisible and recoverable.

---

## What the numbers support, and what they do not

### Solid: v3 improves on v2

Same model, same labels, three metrics moving in the same direction —
accuracy +0.04, precision +0.08, recall +0.17. Three simultaneous improvements
is not one lucky row. The worked examples added in v3 earned their tokens.

### Interesting: the stronger model is worse for this job

Sonnet 5 has the best overall accuracy (0.600 against Haiku's 0.520) and the
worst precision on the class that matters (0.500 against 0.750).

The mechanism is visible in the disagreements. Sonnet committed to
`end_customer_company` on both `ibercsm.net` and `magma.ca`; Haiku committed on
one and returned `unknown` on the other. **Sonnet is more willing to decide,
and decisiveness is the wrong disposition when a wrong commitment reaches a
salesperson.**

Higher accuracy, worse product. Worth stating plainly because the intuitive
move — reach for the better model — is the wrong one here.

### Not supported: most of the differences between configs

`end_customer_company` has support of 6, and each config made only four
predictions in that class. The gap between 0.750 and 0.500 is **one row**.

At n=25 the only difference worth defending is v3-over-v2, because it moved
three metrics at once on a fixed model. Everything else is inside the noise,
and treating three decimal places as stable would be the more serious error
than the low scores themselves.

A set of 100–150, labelled by two people with disagreements adjudicated, is
what these comparisons would need to carry weight.

---

## The failure that is robust

Every configuration, both models, fails identically on **single-host entities
with unfamiliar names**: `gane.com.br`, `provet.in`, `bml.cz`, `web.com`,
`xssl.net`. All returned `unknown` or guessed wrong, in all three runs.

The cause is not the prompt. The human labeller resolved these by looking the
companies up; the model receives one host, a product string, and an
organisation name belonging to whoever owns the IP block. There is genuinely
insufficient evidence in the input to identify a small business.

**This is a confound in the eval as much as a finding about the model.** The
labeller had information the model did not. Two honest readings:

- *The comparison is unfair.* Returning `unknown` at confidence 0.30 on one
  host with no identifying evidence is correct behaviour given those inputs,
  and the prompt explicitly instructs it to prefer `unknown` over a confident
  wrong answer. It is doing as told.
- *The comparison is exactly right.* The product's job is to identify
  companies. If the pipeline cannot, that is a real capability gap, and the
  answer is more evidence rather than a lower bar.

Both are recorded because the second determines what to build next and the
first determines how much to trust the number.

### The fix is evidence, not prompting

HTTP page titles, WHOIS registrant data, or a web-search tool would resolve
most of these. `provet.in` is a veterinary business; one page title would have
said so. Out of scope here, and the highest-value next increment.

---

## A genuine model miss

`ax5z.com` carries `myra security` in its organisation list — Myra Security is
a German DDoS mitigation provider — and every configuration missed it, variously
answering `unknown` or `hosting_or_cloud`.

Unlike the single-host cases, the evidence *was* present and was not used. This
one is fixable in the prompt: an explicit instruction to scan the organisation
list for security-vendor names before classifying. Not applied, because
changing the prompt after seeing the eval and then reporting the same eval is
how a measurement becomes a fiction. It belongs in v4, measured against a
labelled set built after the change.

---

## Cost and latency, measured

| Config | Cost / call | p50 latency | Cache |
|---|---:|---:|---|
| v2 · Haiku | $0.00341 | 1,884 ms | none — block below model minimum |
| v3 · Haiku | $0.00518 | 1,987 ms | none — refused above documented minimum |
| v3 · Sonnet | $0.00378 | 2,673 ms | working — 5,541 tokens read per call |

Across 8,000 entities the spread between cheapest and dearest is about $15.
The configuration choice was made on precision, not price.

---

## Reproducing

```bash
python evals/build_labelled_set.py 25      # worksheet, labels blank
python evals/merge_labels.py               # merge the plain-text labels
python evals/run_eval.py --prompt-version v3 --model claude-haiku-4-5
```

`run_eval.py` reports against the previous stored result automatically and
keys every result on the **(prompt version, model)** pair. Keying on version
alone silently mixed two models inside one version during development and
produced a meaningless average — the configuration is the pair, not either
half.
