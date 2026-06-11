-- Finance asked for total revenue per day. The order date lives on the orders
-- model, so this joins payments to orders and sums by date. Every payment in a
-- day is added together regardless of the currency it was made in.
select
    o.order_date,
    sum(p.amount) as revenue
from {{ ref('stg_payments') }} as p
join {{ ref('stg_orders') }} as o on p.order_id = o.order_id
group by o.order_date
