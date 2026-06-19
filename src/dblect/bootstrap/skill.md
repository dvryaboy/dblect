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

What no dbt test can express, and what you are here to add, is *meaning*: the column
whose type is richer than its SQL type, so that two values which are both
numerically valid still are not interchangeable. dblect propagates that meaning
along the DAG and reports where it shifts (a value changing kind upstream so a
downstream column no longer holds what it claims) before it reaches a dashboard.

Money is the clearest case (a revenue figure quietly going from net to gross, a
currency creeping in), and it is the example most of this guide is written around.
It is one instance of a general idea, not the whole of it.

## What is in scope

Two kinds of meaning propagate along the DAG today. They are what you declare:

- **A magnitude carrying a unit.** A summable quantity together with the tag that
  says what it is measured in, so two values in different units stop being
  interchangeable. Money in a currency is the canonical case, and the one most of
  this guide is written around; a physical quantity in a unit, or a rate, is the same
  shape. Expressed as a `DomainType` (a magnitude facet plus a `UnitEnum` or
  `NominalEnum` tag facet), with **refinements** that fix a meaning-bearing parameter
  (single currency, net vs gross, tax inclusive or not). This is what catches a unit
  changing upstream (`domain_type_contradiction`) and a mixed-unit sum
  (`aggregation_not_well_typed`).
- **A structural invariant over columns.** A functional dependency (an
  `ad -> adset -> campaign` hierarchy where one identifier determines another, or
  every payment on an order sharing the order's currency) or the grain. These let a
  rollup stay well typed. Expressed as `determines` and `grain` facts, **not** as
  types.

A note on closed categories (a `status`, `channel`, `platform`, `country` drawn from
a fixed set). The membership check you would reach for is already covered for free:
an `accepted_values` dbt test on the column becomes a value domain with no
declaration from you. A standalone category column does not yet propagate as a tag on
its own (only a magnitude's tag facet does), so do not mint a bare enum type on a
status column expecting it to catch a meaning shift. A category earns a declaration
only when it rides on a magnitude as that magnitude's unit, as `currency` does on
`Money`.

Restraint is part of the job. Type a column only when one of the two kinds above
genuinely lives in it. A bare identifier is not a type; a key or foreign key is
already read from your dbt test; an unconstrained free-text or one-off numeric column
is just its SQL type. A vouched declaration the data does not support is worse than
none, because dblect trusts it and propagates it. When in doubt, leave it, or ask the
user rather than guess (see "Interview the user").

Two further surfaces are out of scope for this pass, because they do not run on the
`dblect check` path yet: configuration flags (`DomainFlag`) and runnable contract
predicates (an equality with `.within(...)`). Stick to domain types, refinements, and
the fact-returning contracts below.

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
SQL type, across the two kinds that propagate. The high-value candidates:

- **Magnitudes and their units.** Any `amount`, `price`, `revenue`, `cost`, `spend`,
  `total`, or `value` column, and the unit that qualifies it: the `currency` it is
  in, whether it is net or gross, tax inclusive or exclusive, before or after
  discounts. A `Decimal` records none of this. Rates and ratios (`cpc`, `ctr`,
  `rate`, `pct`) are magnitudes too: ask what the base is and what window it covers.
- **Hierarchies and grain.** A containment chain such as `ad` within `adset` within
  `campaign`, or `order_line` within `order`, where one identifier determines
  another. And the grain itself (one row per order, or per order line) when a rollup
  depends on it. Skip plain keys and foreign keys a dbt test already declares; the
  value here is the functional dependency the hierarchy implies, not the keys.

Closed-set columns (`status`, `channel`, `platform`, `country`) are worth noticing
but usually not worth declaring: an `accepted_values` test already guards the set for
free, and a standalone category does not propagate on its own. Spend the effort on
the two kinds above.

Read the SQL to form a hypothesis. A `sum(amount)` grouped by `order_id` tells you
the author believes an order's payments are summable. A currency conversion macro
tells you money flows across currencies. A `group by campaign_id` over an
`ad`-grained model relies on each ad belonging to one campaign. Trace a loaded
column from its source through staging into the marts, and watch for the point where
its meaning could change without the type following.

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
that returns a fact lets the analyzer discharge an obligation it could not see on its
own. The canonical one: every payment on an order shares the order's currency, so
summing an order's payments is well defined.

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

A containment hierarchy is the same fact, applied to identifiers. If each ad belongs
to one adset and each adset to one campaign, then spend summed at the ad level keeps
its campaign, and a `group by campaign_id` stays well defined:

```python
from dblect import ModelContract, contract

class FctAdSpend(ModelContract):
    dbt_model = "fct_ad_spend"

    # One fact per method: each step of ad -> adset -> campaign is its own
    # functional dependency.
    @contract
    def ad_in_one_adset(self):
        return self.ad_id.determines(self.adset_id)

    @contract
    def adset_in_one_campaign(self):
        return self.adset_id.determines(self.campaign_id)
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
