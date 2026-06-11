# Order rollup (sound, no finding)

**The change.** A developer added revenue per order, summing each order's payments:

```sql
select order_id, sum(amount) as revenue
from {{ ref('stg_payments') }}
group by order_id
```

An order is always paid in a single currency, and the team says so with a
functional dependency on the payments contract:

```python
class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    amount: Money.columns(amount="amount", currency="currency")

    @contract
    def one_currency_per_order(self):
        return self.order_id.determines(self.currency)
```

**Why it is sound.** Grouping by `order_id` holds the currency constant within each
group, because the order determines its currency. Summing payments within an order
combines amounts that are all in the same unit.

**What dblect reports.** Nothing. This is the case that keeps a sound-by-default
check from crying wolf: the same `sum(amount)` shape that is wrong by day and by
customer is right by order, and dblect can tell the difference because the
dependency was declared. The summed result keeps its currency, so a later sum
across orders in different currencies would light up on its own.
