# Bootstrap dblect types and contracts for a dbt project

Your job is to read a dbt project, work out which columns carry meaning that plain
validation cannot see, and draft the dblect declaration layer that pins that
meaning. Then run `dblect check` and correct the draft until it resolves cleanly.

dblect already earns its keep with zero declarations: a dozen structural detectors
read the compiled SQL and flag ordering, join, and NULL hazards on their own. It
also reads your existing dbt tests directly. A `unique` or `dbt_utils.unique_combination_of_columns`
test becomes a key fact; a `relationships` test becomes a foreign-key edge; an
`accepted_values` test becomes a value domain. **You do not need to restate any of
that.** Re-declaring keys and foreign keys that a dbt test already states adds
noise and no signal.

What no dbt test can express, and what you are here to add, is *semantics*: that a
column is money in some currency, that a revenue figure is net of tax rather than
gross, that two amounts are the same kind of quantity and may be summed together.
That is the layer dblect propagates along the DAG to catch a meaning shift (a
currency creeping in upstream, a net figure flowing into a gross contract) before
it reaches a dashboard.

## What is in scope

Declare these, and nothing more:

- **Domain types** on the columns whose meaning matters: money, rates, anything
  where two numerically valid values are not comparable.
- **Refinements** that fix a meaning-bearing parameter (single currency, net vs
  gross, tax inclusive or not).
- **Functional-dependency facts** that let a rollup stay well typed (for example,
  every payment on an order shares the order's currency).

Leave out anything you cannot ground in the project's real semantics. A vouched
declaration that the data does not support is worse than no declaration: dblect
trusts it and propagates it. When you are unsure what a column means, ask the user
rather than guess (see "Interview the user").

Two surfaces are deliberately out of scope for this pass, because they do not yet
run on the `dblect check` path: configuration flags (`DomainFlag`) and runnable
contract predicates (an equality with `.within(...)`). Stick to domain types,
refinements, and the fact-returning contracts below.

## Step 1: orient

First confirm how dblect is invoked here. Run `dblect --help`. If it is not on the
PATH, it may live in the project's virtualenv (`.venv/bin/dblect`) or be run through
the project's runner (`uv run dblect`, `poetry run dblect`). Settle this once and use
that form throughout; do not search the filesystem for the package.

Confirm you are in a dbt project (a `dbt_project.yml` exists) and that dblect has a
manifest to read. If `target/manifest.json` is missing, run `dbt compile` yourself,
or let `dblect init` produce it for you (it falls back to `dbt compile`):

```text
dblect init .
```

`init` scaffolds the `dblect/` declaration tree and writes `dblect/_stubs/models.py`.

**Get the column names right, because every binding depends on them.** dblect
resolves a declared column against the project's compiled columns. Two sources feed
that resolution, and they differ in coverage:

- `schema.yml` lists only the columns a human documented. The generated stubs are
  built from it, so they can be blind to undocumented columns (a `stg_payments.amount`
  that no one wrote a description for) and can carry a stale name (a column renamed in
  the SQL but not in `schema.yml`). Treat the stubs as a starting hint, not gospel.
- `target/catalog.json` is the warehouse's own account of every column dbt actually
  emitted. It is the ground truth. dblect reads it when it sits beside the manifest.

So produce the catalog before you rely on resolution. If the models are built,
`dbt docs generate` writes `target/catalog.json`; if not, `dbt build` first. Without
it, undocumented leaf columns (seeds, sources) will not resolve and your bindings
will look like they are missing when they are merely unseen.

Read the project structure: the `models/` tree (usually split into `staging/` and
`marts/`), each model's `.sql`, and the `schema.yml` files that carry column
descriptions and tests. The descriptions are often where a human already wrote down
what a column means.

## Step 2: find the loaded columns

Walk the models and macros looking for columns whose meaning is richer than their
SQL type. The high-value candidates:

- **Money and revenue.** Any `amount`, `price`, `revenue`, `cost`, `total`, or
  `value` column. The meaning that matters is the hidden parameters: which currency,
  net or gross, tax inclusive or exclusive, before or after discounts. A `Decimal`
  tells you none of this.
- **Currencies and units.** A `currency` column, or a money column that should carry
  one. This is the tag that makes a mixed-currency sum ill typed.
- **Rates and percentages.** A `rate`, `pct`, or `ratio` column. What is the base,
  and what window does it cover.
- **Keys and grain, only where semantics add something.** The grain (one row per
  order, or per order line) when a rollup depends on it. Skip keys and foreign keys
  a dbt test already declares.

Read the SQL to form a hypothesis. A `sum(amount)` grouped by `order_id` tells you
the author believes an order's payments are summable. A currency conversion macro
tells you money flows across currencies. Trace a money column from its source
through staging into the marts, and watch for the point where its meaning could
change without the type following.

## Step 3: infer what you can, interview for the rest

Some meaning is readable from the code. A model that filters `where currency = 'USD'`
is single currency by construction. A macro named `to_usd` converts. Lean on the
SQL, the column descriptions, and the dbt test metadata.

The rest you cannot read off the code, and you should not guess it:

- Is this `amount` always one currency, or did the source go multi-currency?
- Is `revenue` net of tax and discounts, or gross?
- Is the grain one row per order, or per order line?

**Interview the user.** Propose your reading of each ambiguous column and ask for
confirmation before you write a vouched fact. For example: "I read `stg_payments.amount`
as USD because the model filters to US orders. Is that still true now that the
source carries a `currency` column?" If you are running in a setting where you
cannot ask, draft the declaration anyway and mark the assumption with a `# TODO:
confirm ...` comment so a human can check it.

## Step 4: write the declarations

Put domain types in `dblect/types.py` and contracts under `dblect/contracts/`. The
examples below are the templates; copy their shape rather than reading dblect's own
source to reverse-engineer it.

**Layout and discovery.** Every `.py` module under `dblect/` is imported, and any
`ModelContract` subclass defined anywhere in that tree registers itself. You do not
wire up imports or a registry. A natural split is one module per model group
(`dblect/contracts/staging.py`, `dblect/contracts/marts.py`), or one per model for a
small project; the location does not matter, only that the class is defined under
`dblect/`.

`Money` is the worked example: an amount and the currency it is denominated in, so
a sum that mixes currencies stops being well typed. It ships in `dblect.demo`.
Declare your own domain type the same way, by subclassing `DomainType` and giving
each facet a typed field:

```python
from dblect.types import Decimal, DomainType, UnitEnum

class Currency(UnitEnum):
    USD = "USD"
    EUR = "EUR"

class Money(DomainType):
    amount: Decimal(18, 2)
    currency: Currency
```

A `UnitEnum` is a tag that must agree when values combine; a `NominalEnum` is a tag
that rides along without that constraint. Use the shipped `dblect.demo.Currency`
slice, or declare your project's own categories as above.

**Refine to fix a meaning-bearing parameter.** A single-currency column pins the
currency; a multi-currency column binds the currency to the column that records it:

```python
from dblect.demo import Currency, Money

# Single currency: pin the tag, so a mixed-currency sum stops being well typed.
RevenueUSD = Money.refine(currency=Currency.USD)

# Multi-currency: bind the currency facet to the column that records it.
PaymentMoney = Money.columns(amount="amount", currency="currency")
```

**Bind types to a model with a `ModelContract`.** Name the model with `dbt_model`
and annotate each column you are typing. A contract with only column bindings and
no methods is valid and already buys type propagation:

```python
from dblect import ModelContract
from dblect.demo import Currency, Money

class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    amount: Money.refine(currency=Currency.USD)
```

**Know the binding rule, or your columns will silently collapse.** This is the one
mechanic worth getting right before you write a contract. A domain type binds its
*magnitude facet* (for `Money`, the field named `amount`) to a warehouse column, and
by default that column is the one named the same as the facet: `amount`. The contract
*field name* on the left does not drive the binding.

So when a model has several money columns whose names are not `amount`, annotating
each with a bare type points all of them at one phantom column called `amount`. They
collapse together and resolve to nothing. Map each one explicitly with `.columns(...)`,
naming the real column the magnitude lives in:

```python
from dblect import ModelContract
from dblect.demo import Currency, Money

MoneyUSD = Money.refine(currency=Currency.USD)

class Orders(ModelContract):
    dbt_model = "orders"
    # `amount` is literally named amount, so the bare refinement binds it.
    amount: MoneyUSD
    # These are not named `amount`, so map the magnitude facet to the real column.
    credit_card_amount: MoneyUSD.columns(amount="credit_card_amount")
    coupon_amount: MoneyUSD.columns(amount="coupon_amount")
    bank_transfer_amount: MoneyUSD.columns(amount="bank_transfer_amount")
```

The rule in one line: bind with the bare type only when the column is named after the
facet; otherwise reach for `.columns(amount="real_column_name")`.

**Add a functional-dependency fact when a rollup needs it.** A `@contract` method
that returns a fact lets the analyzer discharge an obligation it could not see on
its own. The canonical one: every payment on an order shares the order's currency,
so summing an order's payments is well defined.

```python
from dblect import ModelContract, contract
from dblect.demo import Money

class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    value: Money.columns(amount="amount", currency="currency")

    @contract
    def one_currency_per_order(self):
        # The order_id determines the currency, so the per-order rollup is sound.
        return self.order_id.determines(self.currency)
```

The fact vocabulary you can return is small and structural: `determines` (a
functional dependency), `key`, `references`, and `grain`. Reach for one only when a
real invariant in the project supports it.

## Step 5: check and self-correct

Run the checker:

```text
dblect check .
```

**Read the coverage line before the findings.** The check reports how much of what
you declared actually resolved, and that is your first diagnostic. The key counter is
the number of contract columns that resolved against the compiled SQL. If it is lower
than the number of columns you declared, some bindings did not land, even when no
finding fired. The two usual causes are the binding-rule collapse above (a money
column not mapped with `.columns(...)`, so it pointed at a phantom `amount`) and a
missing `catalog.json` (an undocumented column that cannot resolve until the catalog
exists). A grounding count like `domain_type 7/27` is expected and fine: it means you
typed 7 of 27 columns, which is the point, you type only the columns that carry
meaning. Chase the resolved-columns count up to the number you declared; do not chase
grounding up to the total.

Then read the findings and loop. Two kinds matter here:

- **Contract issues** mean a declaration does not line up with the manifest:
  `unresolved_model` (a misspelled or renamed model), `unknown_column` (a column
  that is not on the model), `unsourced_field` (a type facet with no column behind
  it), `out_of_domain_value` (a value outside an enum). Each names the contract and
  field. Fix the declaration so it matches the real names in `_stubs/models.py`.
- **`domain_type_contradiction`** means the meaning you declared conflicts with what
  the DAG carries: a currency creeping in where you pinned USD, a net figure flowing
  into a gross contract. This is the headline catch. Decide whether the declaration
  is stale (the data legitimately changed and the type should open up) or the SQL
  introduced a real bug, and fix the half that is wrong.

Iterate until the contract issues are gone. A remaining `domain_type_contradiction`
may be a true finding worth surfacing to the user rather than silencing; explain it
and let them decide.

## What good looks like

A small, honest declaration layer: domain types on the money and rate columns that
matter, refinements that pin the currency or the net/gross reading you confirmed
with the user, and a functional-dependency fact or two where a rollup needs it. A
handful of contracts, not a wall of them. Every binding grounded in a real column
name and every vouched fact grounded in a real invariant of the project.
