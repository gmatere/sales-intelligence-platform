-- Fit and intent as two separate scores, not one blended number.
--
-- Fit asks "does this match who we sell to" — structural, slow-moving.
-- Intent asks "do they need help right now" — event-driven, fast-moving.
-- Collapsing them into a single ranking destroys the distinction a rep works
-- from: high fit with low intent is a nurture sequence, low fit with high
-- intent is a distraction, and only the high/high quadrant earns a call today.
--
-- Every component is kept as its own column. A rep must be able to see why an
-- account ranked where it did, and "the model said so" loses the call.

with signals as (

    select * from {{ ref('company_signals') }}

),

waf_posture as (

    select
        entity_domain,
        case
            when n_hosts_with_waf = 0 then 'none'
            -- Security specialists are incumbent competitors: displacing them
            -- is a different, longer sale.
            when list_aggregate(list_transform(waf_vendors, v ->
                    case when lower(coalesce(v, '')) similar to
                        '%(fortinet|fortiweb|imperva|incapsula|akamai|kona|zscaler|sonicwall|citrix|netscaler|radware|sucuri|barracuda|f5)%'
                    then 1 else 0 end), 'max') = 1
                then 'security_vendor'
            -- CDN-bundled WAFs are perimeter basics, not a security programme.
            else 'cdn_basic'
        end                                               as waf_posture
    from signals

),

scored as (

    select
        s.*,
        w.waf_posture,

        -- ---------- INTENT: urgency of the finding ----------
        case when s.max_epss >= {{ var('epss_critical') }} then 40
             when s.max_epss >= {{ var('epss_urgent') }}   then 25
             when s.max_epss > 0                            then 10
             else 0 end                                   as pts_epss,
        case when s.n_cves_critical > 0 then 15 else 0 end as pts_critical_cve,
        case when s.n_exposed_datastores > 0 then 20 else 0 end as pts_datastore,
        case when s.heartbleed_vulnerable = 1 then 15 else 0 end as pts_heartbleed,
        case when s.n_remote_access > 0 then 12 else 0 end as pts_remote_access,
        case when s.n_eol_services > 0 then 10 else 0 end  as pts_eol,
        case when s.n_expired_certs > 0 then 10 else 0 end as pts_expired_cert,
        case when s.n_open_directories > 0 then 8 else 0 end as pts_open_dir,
        case when s.n_exposed_cameras > 0 then 8 else 0 end as pts_cameras,
        case when s.n_dead_ssl > 0 then 8 else 0 end       as pts_dead_ssl,
        -- A cert expiring inside 30 days is a dated, concrete reason to call.
        case when s.soonest_cert_expiry_days between 0 and 30 then 8 else 0 end
                                                          as pts_cert_expiring,
        case when s.n_deprecated_tls > 0 then 5 else 0 end as pts_deprecated_tls,

        -- ---------- FIT: match to the ideal customer profile ----------
        -- Mid-market is the target: a real internet estate to defend, but no
        -- in-house SOC. Single-host entities are usually parked domains or one
        -- VPS; very large estates already have a security function and
        -- procurement process.
        case when s.n_hosts = 1               then 5
             when s.n_hosts between 2 and 10   then 25
             when s.n_hosts between 11 and 100 then 40
             when s.n_hosts between 101 and 500 then 30
             else 15 end                                  as pts_size_band,
        case w.waf_posture when 'none' then 25
                           when 'cdn_basic' then 12
                           else 0 end                     as pts_whitespace,
        case when s.n_products >= 2 then 10 else 0 end     as pts_real_estate,
        case when s.n_countries >= 2 then 10 else 0 end    as pts_multi_country,
        case when s.publishes_securitytxt = 0 then 5 else 0 end as pts_low_maturity

    from signals s
    join waf_posture w using (entity_domain)

),

totalled as (

    select
        *,
        least(100, pts_epss + pts_critical_cve + pts_datastore + pts_heartbleed
                 + pts_remote_access + pts_eol + pts_expired_cert + pts_open_dir
                 + pts_cameras + pts_dead_ssl + pts_cert_expiring
                 + pts_deprecated_tls)                    as intent_score,
        least(100, pts_size_band + pts_whitespace + pts_real_estate
                 + pts_multi_country + pts_low_maturity)   as fit_score
    from scored

)

select
    *,

    -- Count of distinct finding categories. Drives the LLM candidate filter:
    -- an entity with no findings is not a prospect regardless of whether it
    -- turns out to be a real company, so it never needs a model call.
    (case when max_epss > 0 then 1 else 0 end)
  + (case when n_eol_services > 0 then 1 else 0 end)
  + (case when n_exposed_datastores > 0 then 1 else 0 end)
  + (case when n_remote_access > 0 then 1 else 0 end)
  + (case when n_expired_certs > 0 then 1 else 0 end)
  + (case when n_self_signed > 0 then 1 else 0 end)
  + (case when n_open_directories > 0 then 1 else 0 end)
  + (case when n_deprecated_tls > 0 then 1 else 0 end)
  + (case when n_exposed_cameras > 0 then 1 else 0 end)  as n_signal_categories,

    case
        when rule_class in ('infrastructure', 'likely_infrastructure')
            then 'X - not a prospect'
        when fit_score >= 50 and intent_score >= 50 then 'A - call now'
        when fit_score >= 50 and intent_score <  50 then 'B - nurture'
        when fit_score <  50 and intent_score >= 50 then 'C - opportunistic'
        else 'D - deprioritise'
    end                                                    as tier

from totalled
