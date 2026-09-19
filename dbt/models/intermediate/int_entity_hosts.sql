-- Attach an entity key to every host and drop records that can never be a
-- prospect. This is the one place the two independent entity anchors are
-- combined, so the precedence rule is auditable in a single expression.
--
-- Hostname-derived domains win over certificate-derived ones: reverse DNS is
-- set by whoever controls the IP, whereas a certificate can be issued for a
-- domain hosted elsewhere. Where there is no PTR record the certificate CN is
-- the only anchor available, and recovering those hosts is worth the weaker
-- provenance — which is why `entity_source` is carried forward rather than
-- discarded.

select
    coalesce(primary_domain, cert_domain)                as entity_domain,
    case
        when primary_domain is not null then 'hostname'
        else 'certificate'
    end                                                  as entity_source,
    h.*

from {{ ref('stg_hosts') }} h

where coalesce(primary_domain, cert_domain) is not null

  -- Honeypots are deliberate decoys. Pitching one means pitching a security
  -- researcher's trap, and the rep finds out on the call.
  and not is_honeypot

  -- RAT controllers and C2 infrastructure are attacker-operated. Not
  -- prospects, and surfacing them would be an active embarrassment.
  and not is_malicious_infra
