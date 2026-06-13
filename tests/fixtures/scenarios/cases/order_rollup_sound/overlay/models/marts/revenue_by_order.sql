-- Revenue per order. An order can be settled across several payments (split
-- across two cards, say), so this rolls those rows into one total. They are all
-- in the order's single currency, so summing them is well defined. We tell dblect
-- that fact with a functional dependency, and the audit stays quiet.
select
    order_id,
    sum(amount) as revenue
from {{ ref('stg_payments') }}
group by order_id
