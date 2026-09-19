-- Honeypots are researcher decoys and RAT/C2 hosts are attacker-controlled.
-- Both are filtered in int_entity_hosts, but that filter is one WHERE clause
-- away from being lost in a refactor, and the failure is invisible: the
-- pipeline keeps working and a rep cold-calls a security researcher's trap.
--
-- Asserting it downstream means the guarantee is tested where it matters
-- rather than trusted where it was written.

select
    entity_domain,
    count(*) as offending_hosts

from {{ ref('int_entity_hosts') }}

where is_honeypot
   or is_malicious_infra

group by 1
