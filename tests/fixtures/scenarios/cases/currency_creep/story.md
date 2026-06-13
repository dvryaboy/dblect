# Currency creep

**The change.** Payments started arriving in more than one currency. A developer
added a `currency` column to the `raw_payments` source and carried it through
`stg_payments`, and updated the source's contract to say so:

```python
class RawPayments(ModelContract):
    dbt_model = "raw_payments"
    value: Money.columns(amount="amount", currency="currency")
```

What they did not touch is the year-old contract on `stg_payments`, which still
asserts every payment is USD:

```python
class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    amount: Money.refine(currency=Currency.USD)
```

**Why it is a bug.** The amount flowing into `stg_payments` now carries a per-row
currency, which contradicts the declared USD. The declaration is a vouched claim
the data no longer supports, and the contradiction does not stay local: it rides
column lineage down to `order_revenue`, a rollup with no contract of its own.

**What dblect reports.** A `domain_type_contradiction` on `stg_payments.amount`,
and the same finding on `order_revenue.revenue`, a model nobody declared anything
about. Typing the two payment models was enough for the framework to find the
blast radius on its own.

**The fix.** Make the stale type honest. Either open the currency
(`Money.columns(amount="amount", currency="currency")`) if the staging model is
genuinely multi-currency now, or convert to USD before the column claims to be USD.
