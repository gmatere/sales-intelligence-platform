-- Fit and intent are sums of hand-tuned component weights capped at 100. If a
-- new component is added without extending the cap, or a weight is made
-- negative, scores silently leave the 0-100 range and the tiering thresholds
-- stop meaning what they say. Fails loudly instead.

select
    entity_domain,
    fit_score,
    intent_score

from {{ ref('company_scores') }}

where fit_score not between 0 and 100
   or intent_score not between 0 and 100
