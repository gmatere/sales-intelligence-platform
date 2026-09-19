-- The candidate set for LLM entity classification, and the artefact that makes
-- the cost model real.
--
-- Two filters stand between 8.9M source records and a model call:
--
--   1. Rules already decided it. Seed-matched and volume-heuristic
--      infrastructure never reaches the model.
--   2. There is nothing to sell. An entity with zero security findings is not
--      a prospect whether or not it is a real company, so its classification
--      is worthless. This is the larger of the two filters and the cheaper
--      one — it is a boolean on an aggregate, not a model call.
--
-- Ordering matters as well as filtering: a token budget that covers only part
-- of the queue should cover the part worth classifying.
--
-- Ordered by fit, not intent. Intent ordering was the first attempt and it was
-- backwards — a multi-tenant estate accumulates every finding belonging to
-- every tenant, so sorting by urgency puts hosting providers at the front. A
-- 25-entity test run classified 20 of them as hosting and exactly one as a
-- real company.
--
-- Fit is the better proxy for "worth asking about": its size band peaks at
-- 11-100 hosts and penalises estates above 500, which is the shape of a
-- company rather than a provider.

select
    entity_domain,

    -- Everything the prompt needs, and nothing it doesn't. Each additional
    -- column is tokens x queue length, so the projection here is a direct
    -- cost decision.
    orgs_seen,
    primary_country,
    n_hosts,
    n_ports,
    n_products,
    products,
    technologies,
    cloud_host_ratio,
    waf_vendors,

    -- Carried for prioritisation and for the downstream brief, not for the
    -- classification prompt itself.
    fit_score,
    intent_score,
    n_signal_categories,
    max_epss,
    headline_cve,
    headline_cve_product,

    rule_class

from {{ ref('company_scores') }}

where rule_class = 'unresolved'
  and n_signal_categories >= 1
  and n_hosts >= {{ var('min_hosts_for_company') }}

order by fit_score desc, intent_score desc
