# Revenue by customer

**The change.** A developer added a lifetime-revenue-per-customer rollup, joining
payments to orders to attribute each payment to a customer:

```sql
select o.customer_id, sum(p.amount) as lifetime_revenue
from {{ ref('stg_payments') }} as p
join {{ ref('stg_orders') }} as o on p.order_id = o.order_id
group by o.customer_id
```

The team has already typed the staging layer and declared the per-order currency
dependency, the same one that makes the order rollup sound:

```python
class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    value: Money.columns(amount="amount", currency="currency")

    @contract
    def one_currency_per_order(self):
        return self.order_id.determines(self.currency)
```

**Why it is a bug.** That dependency holds the currency constant within an order,
not within a customer. A customer transacts across many orders over time, and those
orders can be in different currencies, so summing all of a customer's payments adds
amounts in different units. The grain is wider than the thing the dependency pins
down.

**What dblect reports.** An `aggregation_not_well_typed` finding on
`revenue_by_customer.lifetime_revenue`. This is the case that shows the discharge is
precise: the per-order fix that quiets `revenue_by_order` does not over-apply and
silence a sum at a grain it never made sound. `order_id` determines the currency;
`customer_id` does not, so the customer rollup still lights up.

**The fix.** Convert to a common currency before the rollup, or carry the currency
into the grain and report per customer and currency.
