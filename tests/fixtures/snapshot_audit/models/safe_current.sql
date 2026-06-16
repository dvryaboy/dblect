-- Restricts to the current row, so no fan-out. The detector should stay silent.
select id, status
from {{ ref('orders_snapshot') }}
where dbt_valid_to is null
