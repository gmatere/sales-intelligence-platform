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

_to be filled in as built_

---

## Evals

_to be filled in as built_

---

## Application

_to be filled in as built_
