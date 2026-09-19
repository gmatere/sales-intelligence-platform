{{ config(materialized='view') }}

-- One row per exposed service (ip + port). Cast, rename, and normalise only.
-- No business logic, no filtering, no scoring — those live downstream so the
-- 8.9M-row source is never re-read when scoring rules change.

with raw as (

    select * from read_parquet('{{ var("parquet_glob") }}')

),

typed as (

    select
        ip,
        port,
        transport,
        try_cast(scanned_at as timestamp)                     as scanned_at,
        asn,
        org,
        isp,

        -- Entity anchors, kept separate. `primary_domain` comes from reverse
        -- DNS hostnames; `ssl_cert_cn` is an independent second source that
        -- recovers hosts with no PTR record. Coalescing is a downstream
        -- decision, not an ingest one.
        primary_domain,
        case
            when ssl_cert_cn is null then null
            -- strip wildcard prefix: *.acme.com -> acme.com
            when starts_with(ssl_cert_cn, '*.') then substr(ssl_cert_cn, 3)
            else ssl_cert_cn
        end                                                   as cert_domain,

        -- Normalised org for denylist matching. The raw field carries the same
        -- company under multiple spellings — "Incapsula Inc" vs "Incapsula Inc."
        -- vs "METEVERSE LIMITED" vs "Meteverse Limited." — so exact matching
        -- fails. Lowercase, strip punctuation, drop legal suffixes.
        nullif(trim(regexp_replace(
            regexp_replace(lower(coalesce(org, '')), '[^a-z0-9 ]', ' ', 'g'),
            '\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|sa|bv|pte|pty|plc|lp|ag|as|oy|ab)\b',
            ' ', 'g')), '')                                    as org_normalised,

        country_code,
        country_name,
        region_code,
        city,

        product,
        version,
        os,
        devicetype,
        info,
        cpe23,
        tags,
        http_components,
        scan_module,

        cloud_provider,
        cloud_service,

        http_status,
        http_title,
        http_server,
        http_waf,
        has_securitytxt,

        cves,
        n_cves,
        max_cvss,
        max_epss,
        n_cves_critical,
        n_cves_high,
        n_cves_verified,
        top_cve,
        top_cve_cvss,
        top_cve_epss,
        top_cve_summary,

        ssl_versions,
        ssl_issuer,
        ssl_sig_alg,
        try_cast(ssl_issued as timestamp)                     as cert_issued_at,
        try_cast(ssl_expires as timestamp)                    as cert_expires_at,
        ssl_expired,
        heartbleed,
        services

    from raw

)

select
    *,

    -- Shodan prefixes UNSUPPORTED protocols with '-', so 'TLSv1' means the
    -- server accepts TLS 1.0 while '-TLSv1' means it refuses it. Matching the
    -- bare token is mandatory; a LIKE '%TLSv1%' would invert the meaning.
    coalesce(
        list_contains(ssl_versions, 'TLSv1')
        or list_contains(ssl_versions, 'TLSv1.1'),
        false
    )                                                         as supports_deprecated_tls,

    coalesce(list_contains(ssl_versions, 'SSLv2')
          or list_contains(ssl_versions, 'SSLv3'), false)     as supports_dead_ssl,

    coalesce(ssl_sig_alg ilike '%sha1%', false)               as weak_cert_signature,

    date_diff('day', current_date, cert_expires_at::date)     as days_to_cert_expiry,

    coalesce(list_contains(tags, 'eol-product')
          or list_contains(tags, 'eol-os'), false)            as runs_eol_software,
    coalesce(list_contains(tags, 'self-signed'), false)       as self_signed_cert,
    coalesce(list_contains(tags, 'database'), false)          as exposes_database_tag,
    coalesce(list_contains(tags, 'open-dir'), false)          as open_directory,
    coalesce(list_contains(tags, 'iot'), false)               as iot_device,
    coalesce(list_contains(tags, 'vpn'), false)               as vpn_endpoint,
    coalesce(list_contains(tags, 'cloud')
          or list_contains(tags, 'cdn'), false)               as is_cloud_or_cdn,

    -- Exclusions. Honeypots are decoys and the RAT/malware scan modules mark
    -- attacker-controlled infrastructure. Neither is a prospect, and pitching
    -- either would destroy rep credibility.
    coalesce(list_contains(tags, 'honeypot'), false)          as is_honeypot,
    coalesce(
        scan_module ilike '%-rat%' or scan_module ilike '%rat-%'
        or scan_module ilike '%malware%' or scan_module ilike '%botnet%'
        or list_contains(tags, 'c2') or list_contains(tags, 'compromised'),
        false
    )                                                         as is_malicious_infra,

    -- Remote access and data stores reachable from the public internet.
    len(list_intersect(services,
        ['telnet', 'vnc', 'rdp_encryption', 'ftp', 'smb']))    as n_remote_access_services,
    len(list_intersect(services,
        ['redis', 'mysql', 'mysqlx', 'mongodb', 'elastic', 'postgres', 'ldap']))
                                                              as n_datastore_services,
    len(list_intersect(services,
        ['hikvision', 'dahua_dvr_web']))                      as n_exposed_cameras,

    http_waf is not null                                      as has_waf

from typed
