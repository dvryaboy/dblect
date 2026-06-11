-- Lifetime revenue per customer. A customer can place orders in different
-- currencies over time, so summing all of their payments together adds amounts
-- that are not in the same unit.
select
    o.customer_id,
    sum(p.amount) as lifetime_revenue
from {{ ref('stg_payments') }} as p
join {{ ref('stg_orders') }} as o on p.order_id = o.order_id
group by o.customer_id
