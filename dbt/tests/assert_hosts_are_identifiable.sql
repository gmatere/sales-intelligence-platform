-- Replaces a not_null test on `ip` that encoded a false assumption.
--
-- ~1.2% of records are IPv6-only: Shodan puts the address in `ipv6` and leaves
-- `ip_str` null. Asserting `ip is not null` therefore fails on correct data,
-- and deleting the test outright would remove the guarantee it was reaching
-- for. What actually matters is weaker and true: every host must carry at
-- least one identifier, otherwise it can be neither attributed to a company
-- nor shown to a rep.
--
-- A failure here means records are arriving with no address and no name, which
-- would point at a projection bug rather than a source quirk.

select
    port,
    org,
    scanned_at

from {{ ref('stg_hosts') }}

where ip is null
  and (hostnames is null or len(hostnames) = 0)
  and cert_domain is null
