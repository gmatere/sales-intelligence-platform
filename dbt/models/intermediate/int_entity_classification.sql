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

        avg(case when is_cloud_or_cdn then 1.0 else 0.0 end)     as cloud_host_ratio,
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
    a.orgs_seen,
    a.cloud_host_ratio,
    a.has_hostname_anchor,
    m.matched_provider,
    m.matched_provider_class,

    case
        -- Named in the seed list: settled, no model call needed.
        when m.matched_provider_class is not null then 'infrastructure'

        -- Volume heuristic for providers absent from the seed list. An entity
        -- with hundreds of hosts that are almost entirely cloud/CDN-tagged is
        -- serving other people's traffic, not running its own estate.
        when a.cloud_host_ratio >= 0.9 and a.n_hosts >= 50 then 'likely_infrastructure'

        -- The long tail: regional hosts, VPS resellers, small CDNs, and real
        -- companies, indistinguishable by rule. This is the LLM's job.
        else 'unresolved'
    end                                                    as rule_class,

    case
        when m.matched_provider_class is not null then 'seed_list'
        when a.cloud_host_ratio >= 0.9 and a.n_hosts >= 50 then 'volume_heuristic'
        else null
    end                                                    as rule_evidence

from entity_agg a
left join seed_match m using (entity_domain)
