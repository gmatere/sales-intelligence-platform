-- Rule-based pass at "is this entity a company we could sell to, or is it
-- infrastructure?" — the single most consequential decision in the pipeline.
--
-- This is the cheap tier of the rule-vs-LLM split. A seed list of known
-- providers plus one volume heuristic resolves the obvious cases for free.
-- Everything it cannot decide is marked `unresolved` and handed to the LLM,
-- which is the only tier expensive enough to need rationing.
--
-- Matching is done on the normalised org string, not the raw one: the source
-- carries the same company under several spellings ("Incapsula Inc" vs
-- "Incapsula Inc.", "METEVERSE LIMITED" vs "Meteverse Limited."), so exact
-- comparison silently misses millions of records.

with entity_agg as (

    select
        entity_domain,
        count(*)                                          as n_hosts,
        count(distinct ip)                                as n_ips,
        count(distinct asn)                               as n_asns,

        -- Collapsed into one string so a single LIKE per seed pattern covers
        -- every org this entity appears under. 228k entities x ~70 patterns
        -- is cheap; doing it per host would be 6.5M x 70.
        coalesce(string_agg(distinct org_normalised, ' | '), '') as orgs_seen,

        count(distinct port)                              as n_ports,

        avg(case when is_cloud_or_cdn then 1.0 else 0.0 end)     as cloud_host_ratio,
        avg(case when sequential_hostname then 1.0 else 0.0 end) as sequential_name_ratio,
        max(case when is_reverse_dns_zone then 1 else 0 end)     as is_reverse_dns_zone,
        max(case when entity_source = 'hostname' then 1 else 0 end) as has_hostname_anchor

    from {{ ref('int_entity_hosts') }}
    group by 1

),

seed_match as (

    select
        a.entity_domain,
        min(p.provider)                                   as matched_provider,
        min(p.provider_class)                             as matched_provider_class

    from entity_agg a
    join {{ ref('infrastructure_providers') }} p
      on a.orgs_seen like '%' || p.pattern || '%'
      or a.entity_domain like '%' || replace(p.pattern, ' ', '') || '%'
    group by 1

)

select
    a.entity_domain,
    a.n_hosts,
    a.n_ips,
    a.n_asns,
    a.n_ports,
    a.orgs_seen,
    a.cloud_host_ratio,
    a.sequential_name_ratio,
    a.is_reverse_dns_zone,
    a.has_hostname_anchor,
    m.matched_provider,
    m.matched_provider_class,

    case
        -- Addressing infrastructure. Certain, not a heuristic.
        when a.is_reverse_dns_zone = 1 then 'infrastructure'

        -- Named in the seed list: settled, no model call needed.
        when m.matched_provider_class is not null then 'infrastructure'

        -- Cloud/CDN-tagged at volume: serving other people's traffic.
        when a.cloud_host_ratio >= 0.9 and a.n_hosts >= 50 then 'likely_infrastructure'

        -- Sequential machine naming. In practice this catches ISP address
        -- space more than hosting: customer-premises equipment named by
        -- address (dsl-123-45.isp.net) produces near-total sequential naming.
        --
        -- Threshold raised 0.6 -> 0.8 after sampling. Unambiguous ISPs sit at
        -- 0.87-1.0; the only two false positives in a 25-row sample were the
        -- Italian foreign ministry (esteri.it) and a Belgian aviation firm,
        -- both at exactly 0.64. A false exclusion is far more costly than an
        -- extra model call — an excluded company never gets a second look,
        -- whereas an extra classification costs a fraction of a cent.
        when a.sequential_name_ratio >= 0.8 and a.n_hosts >= 10
            then 'likely_infrastructure'

        -- Multi-tenant estates expose many unrelated services because their
        -- tenants do. A single company with 20 hosts rarely runs 20 distinct
        -- ports; a reseller hosting 20 customers usually does.
        when a.n_hosts >= 20 and a.n_ports >= 20 then 'likely_infrastructure'

        -- The remaining long tail is genuinely ambiguous by rule. Rules can
        -- prove an entity IS infrastructure; they cannot prove it is not.
        -- That asymmetry is what the LLM tier is for.
        else 'unresolved'
    end                                                    as rule_class,

    case
        when a.is_reverse_dns_zone = 1 then 'reverse_dns_zone'
        when m.matched_provider_class is not null then 'seed_list'
        when a.cloud_host_ratio >= 0.9 and a.n_hosts >= 50 then 'cloud_tag_volume'
        when a.sequential_name_ratio >= 0.8 and a.n_hosts >= 10 then 'sequential_hostnames'
        when a.n_hosts >= 20 and a.n_ports >= 20 then 'port_diversity'
        else null
    end                                                    as rule_evidence

from entity_agg a
left join seed_match m using (entity_domain)
