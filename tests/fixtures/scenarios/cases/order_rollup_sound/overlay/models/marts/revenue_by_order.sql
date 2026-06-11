-- Revenue per order. An order is paid in a single currency, so summing its
-- payments is well defined. We tell dblect that fact with a functional
-- dependency, and the audit stays quiet.
select
    order_id,
    sum(amount) as revenue
from {{ ref('stg_payments') }}
group by order_id
