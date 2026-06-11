# Revenue by customer

**The change.** A developer added a lifetime-revenue-per-customer rollup, joining
payments to orders to attribute each payment to a customer:

```sql
select o.customer_id, sum(p.amount) as lifetime_revenue
from {{ ref('stg_payments') }} as p
join {{ ref('stg_orders') }} as o on p.order_id = o.order_id
group by o.customer_id
```

The staging layer is typed as multi-currency money:

```python
class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    amount: Money.columns(amount="amount", currency="currency")
```

**Why it is a bug.** Unlike a single order, a customer can transact in different
currencies over time, so summing all of a customer's payments adds amounts in
different units. This is the case that survives even when per-order sums are sound:
the grain is wider than the thing that holds the currency constant.

**What dblect reports.** An `aggregation_not_well_typed` finding on
`revenue_by_customer.lifetime_revenue`.

**The fix.** Convert to a common currency before the rollup, or carry the currency
into the grain and report per customer and currency.
