-- An entity key must be a domain. IP addresses arrived through the certificate
-- fallback — certificates can legitimately be issued to a bare address — and
-- became "companies" with no organisation behind them.
--
-- Found by reading a sampled eval set rather than by any test, which is the
-- point: this is the shape of bug that passes every schema check because the
-- value is a perfectly valid non-null string.

select
    entity_domain,
    count(*) as hosts

from {{ ref('int_entity_hosts') }}

where regexp_matches(entity_domain, '^[0-9]{1,3}(\.[0-9]{1,3}){3}$')
   or contains(entity_domain, ':')

group by 1
