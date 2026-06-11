# Total daily revenue

**The change.** Finance asked for total revenue per day. A developer added a
`total_daily_revenue` model that joins payments to orders for the date and sums:

```sql
select o.order_date, sum(p.amount) as revenue
from {{ ref('stg_payments') }} as p
join {{ ref('stg_orders') }} as o on p.order_id = o.order_id
group by o.order_date
```

The staging layer is typed as multi-currency money, which is all the team
declared:

```python
class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    amount: Money.columns(amount="amount", currency="currency")
```

**Why it is a bug.** A day contains payments in several currencies, so
`sum(amount)` adds amounts that are not in the same unit. The number looks
plausible and the build stays green, but it is the sum of dollars and euros and
pounds.

**What dblect reports.** An `aggregation_not_well_typed` finding on
`total_daily_revenue.revenue`: the currency companion is not held constant per
group, so reducing the amount is not well typed.

**The fix.** Convert to a common currency before summing, or group by currency as
well so each row of the report is honestly per-currency.
