# Demo walkthrough: jaffle_shop_duckdb

*Status: scaffolds the v1 demo. File contents, command output, and timings are illustrative; they describe what the demo should produce once the framework is implemented to spec, not literal output from a current build.*

This document walks an analyst through dblect end-to-end against the [jaffle_shop_duckdb](https://github.com/dbt-labs/jaffle_shop_duckdb) project. It picks three planted bugs, each one a different *layer of value* dblect uniquely adds, plus one zero-config audit catch that fires before any planting.

## The arc

Five steps, each ending in a finding the existing toolchain (dbt tests + data-diff + Great Expectations) structurally cannot produce.

| # | What we do | What dblect catches | What data-diff would show |
|---|---|---|---|
| 0 | `dblect init` (no declarations) | Latent NULL-group risk in `customers.sql` (zero-declaration static finding) | n/a, no PR yet |
| 1 | Annotate `Money(currency=Currency.USD)` on the critical chain | Nothing yet, types align | n/a, no SQL change |
| 2 | Plant **currency creep** | Type mismatch at PR time, no execution | Scattered CLV value drift, cause invisible |
| 3 | Plant **returns-from-CLV flag** | Conservation contract fails in the flag's True world | Nothing; default unchanged |
| 4 | Plant **apple_pay payment method** | Conservation contract fails under Fanout intent, with shrunk counterexample | The new rows' totals don't reconcile, *if those rows are in the snapshot* |

Three planted bugs, three "what dblect uniquely does" moments: **meaning** (step 2), **configuration space** (step 3), **structural shape** (step 4). Step 0 is the install-day hook.

## Setup

```bash
git clone https://github.com/dbt-labs/jaffle_shop_duckdb
cd jaffle_shop_duckdb
```

The project has three CSV seeds (`raw_customers`, `raw_orders`, `raw_payments`), three staging models, and two marts (`orders`, `customers`). dbt-duckdb is the adapter, so everything runs locally with no warehouse credentials.

## Step 0: `dblect init`

One command:

```bash
$ dblect init
[dblect] Detected dbt project: jaffle_shop
[dblect] Created dblect/ with __init__.py, types.py, contracts/
[dblect] Created .dblect/ cache directory
[dblect] Added .dblect/ to .gitignore
[dblect] Added [tool.dblect] to pyproject.toml
[dblect] Added dblect to [dependency-groups.dev]

[dblect] Installing project dependencies (uv sync)... done in 4.2s
[dblect] Parsing dbt project (dbt parse)... done in 1.8s
[dblect] Generated stubs for 5 dbt models → dblect/_stubs/models.py

[dblect] Running check:
  ✓ Static SQL analysis            5 models       0.4s
  ✓ Ambiguous-ordering detection   5 models       0.2s
  ✓ Replay determinism             5/5 ran        2.1s
  ✓ Heuristic invariants           5/5 ran        3.2s
  ⊘ Airflow task analysis          no Airflow detected

[dblect] Found 1 issue  (1 medium)

  MEDIUM  models/customers.sql:33-45
          NULL-group risk: customer_payments CTE LEFT JOINs payments → orders,
          then GROUP BY orders.customer_id. Any payment with an order_id not
          present in stg_orders produces a row with NULL customer_id; those
          rows aggregate into a single NULL group whose total is then dropped
          by the downstream LEFT JOIN to customers. Silent loss of payment
          totals.
          Suggested: filter `where orders.customer_id is not null` before the
                     GROUP BY, or declare the orphan-handling intent explicitly.

[dblect] Full report: .dblect/check-2026-05-20-101522.html

[dblect] Next steps:
  • Re-run anytime:        dblect check
  • Declare types:         edit dblect/types.py
  • Add contracts:         dblect focus <model>
```

Zero declarations, zero seed planting. dblect reads the SQL, sees the LEFT JOIN feeding a GROUP BY on the right-side key, and warns. The pattern is real (it's been latent in jaffle since the original release) and it's the kind of thing that would silently lose money in a production warehouse if a real orphan payment ever arrived.

**Comparison.** dbt's own `relationships` test on `customer_id` would catch orphan FKs in the source data, but only after the data exists. dblect catches the *structural pattern* in the SQL before any orphan arrives.

## Step 1: declare types on the critical chain

Create `dblect/types.py`:

```python
# dblect/types.py
from dblect import DomainType
from dblect.types import Decimal
from dblect.demo import Currency

class Money(DomainType):
    """A monetary amount in a specified currency."""
    amount: Decimal(18, 2)
    currency: Currency

MoneyUSD = Money.refine(currency=Currency.USD)
```

Create `dblect/contracts/staging.py`:

```python
# dblect/contracts/staging.py
from dblect import ModelContract, Field, ForeignKey
import dblect.types as t
from ..types import MoneyUSD

class StgCustomers(ModelContract):
    dbt_model = "stg_customers"

    customer_id: t.PrimaryKey
    first_name:  t.Varchar
    last_name:   t.Varchar

class StgOrders(ModelContract):
    dbt_model = "stg_orders"

    order_id:    t.PrimaryKey
    customer_id: ForeignKey("stg_customers.customer_id")
    order_date:  t.Date
    status:      t.Varchar

class StgPayments(ModelContract):
    dbt_model = "stg_payments"

    payment_id:      t.PrimaryKey
    order_id:        ForeignKey("stg_orders.order_id")
    payment_method:  t.Varchar
    amount:          MoneyUSD = Field(ge=0)
```

Create `dblect/contracts/marts.py`:

```python
# dblect/contracts/marts.py
from dblect import ModelContract, Field, ForeignKey
import dblect.types as t
from ..types import MoneyUSD

class Orders(ModelContract):
    dbt_model = "orders"

    order_id:             t.PrimaryKey
    customer_id:          ForeignKey("customers.customer_id")
    order_date:           t.Date
    status:               t.Varchar
    credit_card_amount:   MoneyUSD(amount="credit_card_amount")   = Field(ge=0)
    coupon_amount:        MoneyUSD(amount="coupon_amount")        = Field(ge=0)
    bank_transfer_amount: MoneyUSD(amount="bank_transfer_amount") = Field(ge=0)
    gift_card_amount:     MoneyUSD(amount="gift_card_amount")     = Field(ge=0)
    amount:               MoneyUSD = Field(ge=0)

class Customers(ModelContract):
    dbt_model = "customers"

    customer_id:             t.PrimaryKey
    first_name:              t.Varchar
    last_name:               t.Varchar
    first_order:             t.Date
    most_recent_order:       t.Date
    number_of_orders:        t.Integer
    customer_lifetime_value: MoneyUSD(amount="customer_lifetime_value")
```

Run the check:

```bash
$ dblect check
[dblect] Loaded 5 model contracts, 1 DomainType (Money), 0 flags
[dblect] Type propagation across DAG... clean (no mismatches)
[dblect] No contracts declared yet; type-propagation only

[dblect] No findings.
```

The chain is consistent: `Money(currency=Currency.USD)` flows from `stg_payments.amount` through `orders.amount` and the per-method columns, and through `customer_payments` into `customers.customer_lifetime_value`. Nothing is wrong yet. We've installed the immune system; now we'll demonstrate what it sees.

## Step 2: plant currency creep (semantic-meaning correctness)

A PR introduces multi-currency support in the source data without updating the downstream model SQL. This is a real evolution pattern: marketing rolls out international, raw payments start carrying a currency column, but the existing models don't know about it yet.

**Change 1.** Add a `currency` column to `seeds/raw_payments.csv`:

```
id,order_id,payment_method,amount,currency
1,1,credit_card,1000,USD
2,2,credit_card,2000,USD
3,3,coupon,100,USD
4,4,coupon,2500,EUR        ← introduced
5,5,bank_transfer,1700,GBP ← introduced
...
```

**Change 2.** `models/staging/stg_payments.sql`, to pass `currency` through:

```sql
select
    id as payment_id,
    order_id,
    payment_method,
    amount / 100 as amount,
    currency               -- new
from source
```

No annotation changes. Run the check:

```bash
$ dblect check
[dblect] Loaded 5 model contracts, 1 DomainType (Money), 0 flags
[dblect] Type propagation across DAG...

  FAIL  stg_payments.amount [type mismatch]
        Declared: MoneyUSD  (Money refined to currency=Currency.USD)
        Inferred: Money     (currency varies per row; not unifiable with MoneyUSD)
        Reason:   column `currency` on raw_payments observed with values
                  {"USD", "EUR", "GBP"}; `amount` is no longer a single-currency
                  quantity.
        Source:   seeds/raw_payments.csv, models/staging/stg_payments.sql:14

  CASCADE  orders.amount
           orders.credit_card_amount, orders.coupon_amount,
           orders.bank_transfer_amount, orders.gift_card_amount
           customers.customer_lifetime_value
           Type cascade from stg_payments.amount: all downstream MoneyUSD
           annotations now hold mixed-currency values.

[dblect] 1 root finding, 6 cascaded findings.
[dblect] Suggested resolutions:
  • Filter `where currency = 'USD'` in the staging layer if USD-only is intentional.
  • Re-type as `Money` (currency-as-data) and convert via fx_rate at the
    appropriate boundary; update downstream annotations to match.
  • Refine downstream types per-currency (MoneyEUR, MoneyGBP) if the marts
    intentionally fan out by currency.
```

The framework points at the file, the line, the annotation, and three concrete remediations. No data was run; this is pure type propagation from the declared annotation forward through the DAG.

**Comparison to data-diff.** A data-diff between the old run and the new run would show that some CLV values changed (the customers with EUR/GBP orders now have CLVs that mix currencies). The diff might say `customer_lifetime_value: mean shifted 3.2%, 14 rows differ`. It can't say *why* the values changed in a meaning-coherent way, and it has no way to flag that the values, even when they happen to be numerically reasonable, are no longer comparable across customers.

The dblect finding lands at PR review time, before merge. The data-diff finding lands after the production refresh, when someone notices the dashboard looks weird.

**Cleanup.** Revert both changes; check is clean again. The framework remembers the finding in `.dblect/` and would replay it as a regression test if a similar pattern recurs.

## Step 3: plant the returns-from-CLV flag (configuration space)

A dev wants to support excluding returned/returning-pending orders from CLV for an analyst team that prefers "active revenue only." They add it behind a var so the existing default behavior is preserved.

**Change 1.** Add `dblect/flags.py`:

```python
# dblect/flags.py
from dblect import DomainFlag

class ExcludeReturnsFromCLV(DomainFlag):
    """When set, returned/return_pending orders are excluded from CLV."""
    dbt_var = "exclude_returns_from_clv"
    type = bool
    default = False
    # This flag changes computation, not column types; no `affects` clause.
    # The framework still enumerates worlds for contract checking.
```

**Change 2.** Add a CLV-conservation contract on `Customers` (this is the contract the team committed to when they originally built CLV, it just hadn't been written down):

```python
# dblect/contracts/marts.py  (additions)
from dblect import contract, models

class Customers(ModelContract):
    # ... (existing fields) ...

    @contract
    def clv_equals_sum_of_payments(self):
        """CLV reconciles to the sum of all payments for that customer."""
        return (
            self.customer_lifetime_value.sum().group_by(self.customer_id)
            ==
            (
                models.stg_payments.amount.sum()
                .group_by(models.stg_orders.customer_id)
                .joined_on(models.stg_payments.order_id
                          == models.stg_orders.order_id)
            )
        ).within(0.01)
```

**Change 3.** `models/customers.sql`, to gate the `customer_payments` calculation on the flag:

```sql
customer_payments as (

    select
        orders.customer_id,
        sum(amount) as total_amount

    from payments

    left join orders on
         payments.order_id = orders.order_id

    {% if var('exclude_returns_from_clv', false) %}
    where orders.status not in ('returned', 'return_pending')
    {% endif %}

    group by orders.customer_id
),
```

Run the check:

```bash
$ dblect check
[dblect] Loaded 5 model contracts, 1 DomainType (Money), 1 flag (ExcludeReturnsFromCLV)
[dblect] Type propagation across DAG... clean
[dblect] Enumerating flag worlds: 2 worlds (per the boolean domain)
[dblect] Running contracts against intent-driven fixtures...

  world: exclude_returns_from_clv=False
    Customers.clv_equals_sum_of_payments [conservation] ............ PASS

  world: exclude_returns_from_clv=True
    Customers.clv_equals_sum_of_payments [conservation] ............ FAIL
      Tolerance: 0.01
      Failing intent: happy-path baseline (also fails under all applicable intents)
      Shrunk counterexample (3 rows across 3 tables):
        customers: {customer_id: 1, ...}
        orders:    {order_id: 1, customer_id: 1, status: 'returned'}
        payments:  {payment_id: 1, order_id: 1, amount: 50.00, method: credit_card}
      Result:
        customer_lifetime_value (per contract): 0.00
          (returned order excluded under flag=True)
        sum(stg_payments.amount) per customer:  50.00
          (returned payment still counted on the RHS)
        delta: 50.00

      Diagnosis: the contract reconciles CLV against all payments, but the
      flag-gated SQL excludes payments for returned orders from the CLV side.
      Either the contract is wrong (CLV should exclude returns when the flag
      is on) or the SQL change is wrong (CLV should still include returned
      payments). Decide and update both halves.

[dblect] 1 world failed, 1 world passed.
```

The framework runs the contract under *both* possible values of the flag, even though only one is currently active in `dbt_project.yml`. The True world breaks the conservation; the False world passes. The finding includes the specific world and a shrunk three-row example showing why.

**Comparison to data-diff.** Data-diff would show no change. The flag defaults to False, so the current dbt run produces identical output to before the PR. The bug ships, the flag sits unused for six months, then someone flips it for an analyst experiment and the dashboards quietly break. dblect catches it at PR review time, before merge.

**Cleanup.** Revert; check is clean.

## Step 4: plant apple_pay (structural shape under adversarial inputs)

A new payment method joins the platform. The product team adds rows to the source data and updates the staging accepted_values, but misses the hard-coded Jinja list in `orders.sql`.

**Change 1.** Add a few `apple_pay` rows to `seeds/raw_payments.csv`:

```
115,5,apple_pay,800
116,7,apple_pay,2500
117,8,apple_pay,500
```

**Change 2.** `models/staging/schema.yml`, to update the `accepted_values` test:

```yaml
- name: payment_method
  tests:
    - accepted_values:
        arguments:
          values: ['credit_card', 'coupon', 'bank_transfer', 'gift_card', 'apple_pay']
```

**Change 3.** Add a per-method-conservation contract to `Orders` (this is what the model's design implicitly committed to, where total = sum of per-method amounts; it just hadn't been written down):

```python
# dblect/contracts/marts.py  (additions to Orders)
class Orders(ModelContract):
    # ... (existing fields and contracts) ...

    @contract
    def per_method_amounts_sum_to_total(self):
        """Per-method amounts reconcile to the order's total amount."""
        return (
            self.credit_card_amount
            + self.coupon_amount
            + self.bank_transfer_amount
            + self.gift_card_amount
            == self.amount
        ).within(0.01)
```

Note we deliberately don't change `orders.sql`. The Jinja list still has only the original four methods. dbt's accepted_values test passes (because we updated it). dbt builds successfully. The bug is invisible to dbt.

Run the check:

```bash
$ dblect check
[dblect] Loaded 5 model contracts, 1 DomainType (Money), 1 flag
[dblect] Type propagation across DAG... clean
[dblect] Enumerating flag worlds: 2 worlds
[dblect] Running contracts against intent-driven fixtures...

  world: exclude_returns_from_clv=False
    Customers.clv_equals_sum_of_payments [conservation] ............ PASS
    Orders.per_method_amounts_sum_to_total [conservation]
      ├── intent: Fanout(N=2) ......................................... FAIL
      ├── intent: Fanout(N=3) ......................................... FAIL
      ├── intent: Orphan(side=payments) ............................... PASS
      ├── intent: Orphan(side=orders) ................................. PASS
      ├── intent: NullKey(side=payments) .............................. PASS
      └── intent: EmptyGroup .......................................... PASS

      Failing intent: Fanout(N=2) on payments → orders
      Shrunk counterexample (3 rows across 2 tables):
        orders:
          {order_id: 1, customer_id: 1, order_date: 2024-01-01,
           status: 'completed'}
        payments:
          {payment_id: 1, order_id: 1, payment_method: 'apple_pay',  amount: 50.00}
          {payment_id: 2, order_id: 1, payment_method: 'credit_card', amount: 25.00}
      Result for order_id=1:
        credit_card_amount   = 25.00
        coupon_amount        =  0.00
        bank_transfer_amount =  0.00
        gift_card_amount     =  0.00
        sum(per-method)      = 25.00
        amount               = 75.00   ← includes apple_pay
        delta                = 50.00

      Diagnosis: total `amount` is sum(payments.amount) over all methods
      (including 'apple_pay'); per-method columns are computed from a Jinja
      list ['credit_card','coupon','bank_transfer','gift_card'] in
      models/orders.sql:1 that does not include 'apple_pay'. Per-method
      sums diverge from the total whenever an apple_pay payment exists.

      Suggested:
        • Add 'apple_pay' to the {% set payment_methods %} list, or
        • Generate the list dynamically:
            {% set payment_methods = dbt_utils.get_column_values(
                table=ref('stg_payments'), column='payment_method') %}

[dblect] 1 contract failed (1 world, 2 intents).
```

The contract is straightforward analytic algebra. The framework's contribution is that it ran the contract under the **Fanout(N=2)** intent (one order with two payments, one of which is the new apple_pay method) and the inevitable inconsistency surfaced. A hand-written dbt test might have caught this *if* the author had thought of apple_pay specifically; intent-driven generation finds it because Fanout is exactly the structural shape that exercises the per-method/total reconciliation.

**Comparison to data-diff.** Data-diff catches the symptom only if the snapshot it compares includes apple_pay rows. On the day the PR lands, the seed has apple_pay rows, so the diff between the previous build (no apple_pay) and this build shows the per-method columns changed for affected orders. But the diff doesn't surface the *contract* that's now broken, and if the apple_pay rows happen to be filtered out of the comparison sample, the diff sees nothing.

**Cleanup.** Either revert the seed addition and the contract, or actually fix `orders.sql`. The point is made.

## What we showed

Three planted bugs, three different "this is what dblect uniquely does" moments, each one outside data-diff's structural reach in a different way:

1. **Currency creep**: meaning-level catch, no execution needed. Data-diff sees value drift; dblect names the type contract that broke and points at the source.
2. **Returns-from-CLV flag**: configuration-space catch. Data-diff sees nothing (default branch unchanged); dblect enumerates both worlds and identifies which one breaks the conservation contract.
3. **Apple_pay conservation**: structural-shape catch under adversarial inputs. Data-diff sees the symptom only on shapes present in the snapshot; dblect's intent catalog generates the Fanout shape on purpose and surfaces the latent bug with a minimal counterexample.

Plus an audit finding before any planting: the latent NULL-group risk in `customers.sql` that's been in jaffle since the original release.

## The final `dblect/` tree

After the demo, with all bugs reverted but declarations kept:

```
dblect/
├── __init__.py
├── types.py
├── flags.py
├── contracts/
│   ├── __init__.py
│   ├── staging.py
│   └── marts.py
└── _stubs/
    └── models.py        # autogenerated
.dblect/                 # gitignored: counterexample DB, parsed-manifest cache
```

About 80 lines of Python total. Three contracts. One domain type. One flag. That's the standing investment that catches the three bug classes above and any future variant of them.

## What to add to the demo next

These are deferred but worth doing once the v1 framework is up:

- **A flag-flip preflight pass.** Show `dblect impact --flag exclude_returns_from_clv` listing every contract and column affected before the flag is flipped in production.
- **A typed counterexample replay.** After step 4, show `dblect show-case <id>` materializing the shrunk apple_pay fixture in DuckDB so the developer can run the bug interactively.
- **PR-mode integration.** Show the dblect output rendered as GitHub PR annotations on the actual bug-planting commit.
- **A multi-currency follow-through.** After step 2, show what it looks like to *fix* the bug properly: re-type `amount` as `Money` (currency-as-data), declare a per-customer currency-coherence contract, watch the framework validate the fix under the same data.

These extensions are demo extensions, not v1 framework extensions. The framework supports them all; they just take more screen time than the headline arc.
