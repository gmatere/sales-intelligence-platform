-- The most expensive failure this product can have is putting Cloudflare or
-- AWS in a rep's call list. Infrastructure must always land in the
-- 'X - not a prospect' tier regardless of how high its exposure scores, and
-- exposure scores for infrastructure are high by construction — a CDN fronting
-- thousands of sites accumulates every finding its customers have.
--
-- So this is not a redundant check on the CASE expression: it guards the one
-- ordering dependency in the tiering logic, where a rule reordered to put fit
-- and intent first would silently promote every provider to tier A.

select
    entity_domain,
    rule_class,
    tier,
    fit_score,
    intent_score

from {{ ref('company_scores') }}

where rule_class in ('infrastructure', 'likely_infrastructure')
  and tier != 'X - not a prospect'
