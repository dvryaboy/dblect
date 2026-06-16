-- The snapshot is read in a CTE that carries no filter; the temporal restriction
-- lives in the outer query over the CTE. This is a correct, common shape, so the
-- detector should stay silent (it must look up the enclosing scope chain, not
-- only the snapshot's immediate SELECT).
with history as (
    select * from {{ ref('orders_snapshot') }}
)
select h.id, h.status
from history h
where h.dbt_valid_to is null
