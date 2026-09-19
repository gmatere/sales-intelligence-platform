{{ config(severity='warn', warn_if='>0', error_if='>100000') }}

-- Counts host records with no usable identity: no IPv4 address (IPv6-only,
-- and `ipv6` is not projected), no reverse-DNS hostname, and no certificate
-- CN. Measured at 81,945 — 0.9% of the source.
--
-- Warn rather than error, for two reasons. Staging is a faithful projection of
-- the source, so records the source cannot identify belong here; and the
-- guarantee that actually matters is enforced one layer down, where
-- int_entity_hosts drops them and not_null on entity_domain proves it.
--
-- Not deleted, because a silent count is worse than a noisy one. The error
-- threshold sits just above the known baseline: if unidentifiable records grow
-- past 100k the source has changed shape — a new scan module, a projection
-- regression, or IPv6 coverage expanding — and that should stop the build
-- rather than quietly shrink the addressable market.

select
    port,
    org,
    scanned_at

from {{ ref('stg_hosts') }}

where ip is null
  and (hostnames is null or len(hostnames) = 0)
  and cert_domain is null
