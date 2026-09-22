# Planning — the use cases, and why these

Written before the build, revised once when the data contradicted an
assumption. The revision is kept visible rather than tidied away.

---

## How B2B sales teams actually prospect

Four concepts from the research shaped what got built.

**Ideal Customer Profile.** Criteria defining who you sell to — firmographic,
technographic, behavioural. An ICP is a filter, not a ranking.

**Fit versus intent.** Two independent axes. *Fit* asks whether a company
matches the profile: structural, slow-moving. *Intent* asks whether they need
help right now: event-driven, fast. Reps work the high-fit/high-intent quadrant
first; high fit with low intent is a nurture sequence, and low fit with high
intent is a distraction.

**Buying signals.** Events that create timing — a breach, a compliance
deadline, a system reaching end of life. Distinct from fit, and much more
perishable.

**Whitespace.** Accounts with no incumbent vendor. Displacing a competitor is a
longer, different sale than selling into an empty seat.

**The three questions.** Before dialling, a rep needs to know why this account,
why now, and what to say. A tool that answers those three is used; one that
shows a score and a data dump is not.

---

## What the dataset actually is

Not company data. **Shodan internet-wide scan banners** — one record per
exposed service on an IP address, 8,914,693 of them, snapshotted 2026-09-14.

This was established in the first ten minutes by pulling 8 MB of the compressed
file and profiling it, before any design work. It invalidated the firmographic
approach the brief's framing suggested and saved a day of building against the
wrong shape.

The useful consequence: **exposure data is almost pure intent signal.** It says
nothing about company size, industry or revenue, and a great deal about whether
something is broken right now. That asymmetry set the product.

---

## The use cases chosen

### 1. Entity resolution — identify who the prospect actually is

Not a use case anyone asks for, but everything depends on it, so it is the
primary one.

The dataset's ownership field names whoever registered the IP block, which for
two thirds of records is a hosting provider. Group by it and the top prospects
are Google, Imperva, Cloudflare and Amazon.

The entity key is therefore the registrable domain extracted from reverse DNS,
with the TLS certificate CN as a second anchor. 8.9M records collapse to
252,078 entities.

**Why first:** every other use case is downstream. A prospect list that
contains cloud providers is not a weaker list, it is an unusable one — the
first rep to phone a datacenter about its customers' vulnerabilities stops
trusting the tool.

### 2. Account scoring — fit and intent, kept separate

Two independent 0–100 scores, crossed into a tier.

**Fit** comes from estate size (a proxy for company size — the only one
available), market, whether an incumbent WAF vendor is present, and technology
footprint. The size band peaks at 11–100 hosts: large enough to have something
to defend, small enough to have no in-house security team.

**Intent** comes from urgency of finding — exploitation probability, exposed
databases, remote access, certificates expiring inside 30 days.

**Why separate:** a blended score destroys the distinction a rep works from.
Every contributing component is kept as its own column so a ranking can be
explained rather than asserted.

### 3. Buying signals — derived from technical exposure

The creative core. What in a scan record tells a salesperson this company needs
help now?

| Signal | Why it is a buying signal |
|---|---|
| High-EPSS vulnerability | A bug with a published probability of being exploited within 30 days |
| Exposed database | A public MySQL, Redis or Elasticsearch — immediate and indefensible |
| Remote access exposed | Telnet, RDP, VNC or FTP reachable from the internet |
| End-of-life software | Past vendor support, so no patch is coming |
| Certificate expiring | A dated, concrete reason to call this month |
| Deprecated TLS | TLS 1.0/1.1 still accepted — a PCI-DSS finding |
| Exposed cameras | Hikvision and Dahua interfaces, a well-known entry point |
| No WAF anywhere | Whitespace — no incumbent to displace |

**The decision that matters here: EPSS, not CVE counts.** Every CVE in this
dataset is version-inferred — Shodan matches a version banner against
advisories without testing the host, and `verified` was false on 100% of 7,066
sampled entries. Two hosts both reporting CVSS 9.8 had exploitation
probabilities of 0.99999 and 0.01225: identical severity, completely different
urgency. Ranking on CVE count produces nonsense and outreach that leads with
"you have 99 vulnerabilities" is usually wrong.

### 4. Territory and segment filtering

Markets, estate-size bands, urgency floor, whitespace-only, actively-exploited-
only. A rep who cannot sell into a region should not see it.

One filter was added after seeing the output rather than before: **commercial
versus public sector.** Nine of the top fifteen accounts turned out to be
universities and government research institutes, because those estates are
large, old and heterogeneous, so they accumulate findings. They are genuine
buyers on a completely different motion — procurement and tenders, not a cold
call. Surfaced as a filter rather than suppressed.

### 5. Outreach prioritisation — the three questions

The account view answers them directly. *Why now* lists findings with an
evidence column separating **observed** from **tested** from **inferred**, so
nobody leads a call with a version-inferred CVE as though it were confirmed.
*Why this account* shows the score with every contributing component. *What to
say* drafts an opener grounded in the specific finding, hedged on anything
inferred.

---

## Deliberately out of scope

**Firmographic enrichment.** Headcount, revenue and industry are absent from
the source and would need a paid provider. Estate size is a weak substitute and
is documented as such.

**Cross-domain company resolution.** `acme.com` and `acme.co.uk` stay separate.
Merging needs firmographic data or embeddings — real work, not a weekend's.

**LLM-written outreach.** Openers are template-assembled from the specific
finding. Accurate and correctly hedged, but they will read alike across
accounts sharing a finding type. The obvious next increment.

**Real-time scanning.** One snapshot. Nothing here refreshes.

---

## What I would build next, in order

**1. More evidence for entity resolution.** The measured failure mode is
single-host entities with unfamiliar names — `provet.in` is a veterinary
practice, but the model sees one host, nginx, and someone else's org name. HTTP
page titles, WHOIS registrant data or a search tool would resolve most of them.
This is the highest-value increment, and it is an evidence problem rather than
a prompting one.

**2. A larger eval set — partially done, and it mattered.** The set was grown
from 25 to 75 and the configuration ranking inverted: the version recommended
at n=25 came last at n=75. Support is now 18 on the headline class, which is
better and still not enough. 150+ with two labellers and adjudicated
disagreements is the next step.

**3. Tier thresholds set from the distribution.** 779 of 972 confirmed
prospects land in tier A, because the queue is already filtered to entities
with a finding, so the urgency threshold no longer discriminates. Percentiles
rather than absolute cuts.

**4. LLM-drafted openers** for the top accounts, with the same eval discipline
applied to them as to classification.
