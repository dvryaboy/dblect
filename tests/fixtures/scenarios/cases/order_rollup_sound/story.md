# Order rollup (sound, no finding)

**The change.** A developer added revenue per order, summing each order's payments:

```sql
select order_id, sum(amount) as revenue
from {{ ref('stg_payments') }}
group by order_id
```

An order can be settled across more than one payment, for instance split across
two cards, so this genuinely rolls several payment rows up into one order total.
All of an order's payments are in the same currency, though, and the team says so
with a functional dependency on the payments contract:

```python
class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    value: Money.columns(amount="amount", currency="currency")

    @contract
    def one_currency_per_order(self):
        return self.order_id.determines(self.currency)
```

**Why it is sound.** An order's payments can span several rows, but grouping by
`order_id` holds the currency constant within each group, because the order
determines its currency. Summing those rows combines amounts that are all in the
same unit.

**What dblect reports.** Nothing. This is the case that keeps a sound-by-default
check from crying wolf: the same `sum(amount)` shape that is wrong by day and by
customer is right by order, and dblect can tell the difference because the
dependency was declared. The summed result keeps its currency, so a later sum
across orders in different currencies would light up on its own.
