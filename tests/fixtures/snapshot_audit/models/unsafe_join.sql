-- Reads the snapshot under a JOIN with no temporal filter: fans out one row per
-- historical version. The detector should flag this.
select s.id, o.status
from {{ ref('orders_snapshot') }} s
join {{ ref('raw_orders') }} o on s.id = o.id
