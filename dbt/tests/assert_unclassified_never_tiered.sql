-- An entity the rules could not classify is not a confirmed company, and must
-- not appear in a rep's call list before the LLM has adjudicated it.
--
-- This exists because the first build got it wrong in exactly this way. The
-- tiering logic treated "no evidence of infrastructure" as "is a prospect",
-- and tier A filled with regional hosting providers — Beget, Forpsi,
-- startdedicated, vps-10 — plus one reverse-DNS zone. Every one of them scored
-- 90 fit and 100 intent, because a multi-tenant estate accumulates the
-- findings of everyone it hosts.
--
-- The scores were right. The conclusion drawn from them was not.

select
    entity_domain,
    rule_class,
    tier,
    fit_score,
    intent_score

from {{ ref('company_scores') }}

where rule_class = 'unresolved'
  and tier not in ('U - unclassified')
