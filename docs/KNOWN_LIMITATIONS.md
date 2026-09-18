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

_to be filled in as built_

---

## LLM layer

_to be filled in as built_

---

## Evals

_to be filled in as built_

---

## Application

_to be filled in as built_
