-- Revenue per order. No contract is written for this model; the type the team
-- declared upstream travels here on its own, which is how a stale assumption
-- becomes a finding on a model nobody touched.
select
    order_id,
    sum(amount) as revenue
from {{ ref('stg_payments') }}
group by order_id
