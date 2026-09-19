-- One row per entity, with every observable security finding aggregated.
-- Deliberately unscored: this model answers "what is true about this company",
-- and `company_scores` answers "how much should a rep care". Keeping them
-- apart means the scoring weights can be retuned without recomputing signals.

with hosts as (

    select * from {{ ref('int_entity_hosts') }}

),

aggregated as (

    select
        entity_domain,

        -- Footprint. Host count is the only size proxy available, and it
        -- doubles as the ICP size band downstream.
        count(*)                                          as n_services,

        -- IPv6-only hosts have a null `ip` (Shodan populates `ipv6`, which is
        -- not projected), so a plain count(distinct ip) drops them and
        -- understates the estate for ~1.6% of entities. Falling back to the
        -- first hostname keeps multi-port IPv6 hosts collapsed to one host
        -- rather than counting each service separately.
        count(distinct coalesce(ip, hostnames[1]))        as n_hosts,
        count(distinct port)                              as n_ports,
        count(distinct product)                           as n_products,
        count(distinct country_code)                      as n_countries,

        mode(country_code)                                as primary_country,
        mode(country_name)                                as primary_country_name,
        mode(region_code)                                 as primary_region,
        mode(city)                                        as primary_city,
        list_distinct(list(country_code))                 as countries,

        -- Urgency. EPSS leads because it is the only vulnerability signal here
        -- that is not a version inference: it is a published probability that
        -- the flaw is being exploited, independent of whether this host is
        -- actually affected.
        max(max_epss)                                     as max_epss,
        max(max_cvss)                                     as max_cvss,
        sum(n_cves_critical)                              as n_cves_critical,
        sum(n_cves_high)                                  as n_cves_high,
        sum(n_cves_verified)                              as n_cves_verified,
        sum(n_cves)                                       as n_cve_findings,

        -- Software hygiene
        sum(case when runs_eol_software then 1 else 0 end)        as n_eol_services,
        sum(case when supports_deprecated_tls then 1 else 0 end)  as n_deprecated_tls,
        sum(case when supports_dead_ssl then 1 else 0 end)        as n_dead_ssl,
        sum(case when weak_cert_signature then 1 else 0 end)      as n_weak_cert_sig,
        sum(case when self_signed_cert then 1 else 0 end)         as n_self_signed,
        sum(case when ssl_expired then 1 else 0 end)              as n_expired_certs,
        min(days_to_cert_expiry)                                  as soonest_cert_expiry_days,

        -- Exposure severity
        sum(n_datastore_services)                         as n_exposed_datastores,
        sum(n_remote_access_services)                     as n_remote_access,
        sum(n_exposed_cameras)                            as n_exposed_cameras,
        sum(case when open_directory then 1 else 0 end)   as n_open_directories,
        sum(case when iot_device then 1 else 0 end)       as n_iot_devices,
        sum(case when vpn_endpoint then 1 else 0 end)     as n_vpn_endpoints,
        max(case when heartbleed = 'VULNERABLE' then 1 else 0 end) as heartbleed_vulnerable,

        -- Incumbent vendor / whitespace. A company with no WAF anywhere across
        -- its estate has no perimeter vendor to displace.
        count(http_waf)                                   as n_hosts_with_waf,
        list_distinct(list(http_waf))                     as waf_vendors,
        max(case when has_securitytxt then 1 else 0 end)  as publishes_securitytxt,

        -- Technographics
        list_distinct(flatten(list(http_components)))     as technologies,
        list_distinct(list(product))                      as products,

        -- The single most urgent finding, for the outreach hook. Picked by EPSS
        -- rather than CVSS: a moderate bug under active exploitation is a more
        -- pressing conversation than a critical one nobody is attacking.
        arg_max(top_cve, coalesce(top_cve_epss, 0))       as headline_cve,
        max(top_cve_epss)                                 as headline_cve_epss,
        arg_max(top_cve_cvss, coalesce(top_cve_epss, 0))  as headline_cve_cvss,
        arg_max(top_cve_summary, coalesce(top_cve_epss, 0)) as headline_cve_summary,
        arg_max(product, coalesce(top_cve_epss, 0))       as headline_cve_product,
        arg_max(version, coalesce(top_cve_epss, 0))       as headline_cve_version,

        max(scanned_at)                                   as last_seen_at

    from hosts
    group by 1

)

select
    a.*,
    c.rule_class,
    c.rule_evidence,
    c.matched_provider,
    c.matched_provider_class,
    c.cloud_host_ratio,
    c.orgs_seen,

    -- Whitespace flag, stated positively so the app can filter on it.
    a.n_hosts_with_waf = 0                                as no_waf_anywhere

from aggregated a
left join {{ ref('int_entity_classification') }} c using (entity_domain)
