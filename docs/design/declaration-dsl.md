# The declaration DSL: authoring domain types and model contracts

*Status: design notes, consumer-experience focused. This document fixes the authoring surface a dblect user actually writes and reads: domain types, refinements, and the model contracts that bind them to dbt models. It sits above the substrate ([lineage-facts.md](lineage-facts.md)) and is a companion to the broader [technical intro](dblect_technical_intro.md), narrowed to one question: what does it feel like to declare meaning in dblect, and watch the framework catch where that meaning stops composing? Syntax shown is the target; the genuinely unsettled surface details are listed at the end.*

## The promise this surface keeps

A dbt project encodes meaning in SQL and in the analysts' heads, and almost nowhere a tool can read. `order_total` is net of discounts and gross of tax; `revenue` switched from accrual to cash basis last quarter; `amount` is dollars until the day someone adds a EUR row. None of that is written down, so when one of these meanings shifts the build stays green and the dashboards drift.

dblect's declaration layer is where you write the meaning down, once, in Python that sits beside your dbt project and never touches your models. You declare what a column *means* (a domain type), bind it to the dbt model that produces it (a model contract), and the framework propagates those meanings along the dbt DAG. The payoff is not the declaration; it is the moment the framework flags the place where two meanings collide or an operation stops making sense, at PR review time, before any data runs.

This document is about the writing of it, and about the collisions that motivate the whole surface: meanings that fail to combine across columns or across rows, and a magnitude a join quietly double counts. The companion docs cover what the framework does mechanically with what you write.

## Why it looks like Pydantic

If you have written a Pydantic model, you can read every declaration here on first sight. That is deliberate, and it is the whole reason the surface is Python rather than YAML.

```python
# This is Pydantic. You already know how to read it.
class Order(BaseModel):
    order_id: int
    customer_id: int
    total: Decimal = Field(gt=0)
```

```python
# This is dblect. Same shape, same instincts.

# 1. Define what a column means. A DomainType is a record of fields; here, one.
#    (CustomerId and RevenueNet, used below, are defined the same way.)
class OrderId(dblect.DomainType):
    value: int                              # the one field maps to the column

# 2. Bind those types to a dbt model's columns. This is a ModelContract.
class FctOrders(dblect.ModelContract):
    dbt_model = "marts.fct_orders"

    order_id:    OrderId                            # mirrors `order_id: int`
    customer_id: CustomerId
    order_total: RevenueNet = dblect.Field(gt=0)    # mirrors `total: Decimal = Field(gt=0)`
```

Class-shaped declarations, type-annotated fields, `Field(...)` metadata, the class as both the thing you author and the structured data the framework introspects. That pattern is Pydantic's gift to the Python ecosystem, refined over years of production use, and an entire generation of data tools (Pandera, SQLAlchemy, Ibis, dlt, Prisma) has inherited it. dblect stands in that lineage on purpose: autocomplete, type-checking, jump-to-definition, and refactor-rename all come for free because your editor already understands classes and annotations.

One honest caveat, stated up front because it is the only place a Pydantic reader's instinct needs adjusting:

> **dblect declarations look like Pydantic, but they are never instantiated.** `DomainType` and `ModelContract` use their own metaclass and carry none of Pydantic's per-value validation machinery. We borrow the *shape* (annotated fields, `Field` metadata, `Annotated` constraints, class-as-data), not the runtime. Pydantic fills a model's fields with one row's values at runtime; dblect reads the fields as a schema and maps them onto a SQL relation, statically. The next section is the one idea that follows from that.

## The one idea: every field is a column, and its value comes from the data or the type

A Pydantic model is a record of fields, and so is a dblect `DomainType`. The single thing to learn is where each field's value comes from.

Pydantic gives every field a value, one record per row, when the program runs. dblect never runs the record; it reads the fields as a schema over a SQL relation. Every field is a column of that relation, and a column's value comes from one of exactly two places:

- a **physical column**, when the value varies row to row and is stored in the warehouse, or
- a **logical column**, when the value is the same for the whole column in a given build and comes from your declarations (fixed in the type, or chosen by a dbt var), so the warehouse stores nothing for it.

That single degree of freedom is the entire conceptual delta, and it is what lets one type describe different shapes in different projects. Take money:

```python
import dblect
from dblect.types import Decimal, Currency   # Currency is the ISO 4217 enum

class Money(dblect.DomainType):
    """An amount of money in some currency."""
    amount:   Decimal(18, 2)
    currency: Currency
```

`Money` has two fields. Where their values come from depends on the project, not on the type:

- **Single-currency project.** Every monetary column is dollars. You fix `currency` in the type: `Money(currency=Currency.USD)`. Now `amount` is a physical column and `currency` is a logical column the framework carries. There may be no `currency` column in the warehouse anywhere, and that is correct.
- **Multi-currency project.** Currency varies per row. You leave `currency` open, so both fields are physical columns (`amount` and `currency`). A `Money` value is now genuinely a pair of columns that travel together.

The same field, `currency`, is a logical column in one project and a physical column in the next. Nothing in the type marks it as special; whether a field's value comes from the data or from the type is a property of how the type is *used*, not of how it is *defined*. This is exactly why the model has no separate concept for value-fields versus label-fields: there is one concept, a column, and two sources for its value.

The same idea explains a field whose value the warehouse, in practice, never stores:

```python
class Revenue(dblect.DomainType):
    """A revenue amount, with what it includes recorded as part of the type."""
    amount:            Decimal(18, 2)
    contains_tax:      bool
    contains_discount: bool
    currency:          Currency
```

No warehouse stores a per-row `contains_tax` boolean; it is the same value on row 1 and row 1,000,000. So in every real project `contains_tax` is a logical column: its value comes from the type, recorded so the framework can reason about it. It is not a special kind of field, just an ordinary one whose value the warehouse never stores. The reasoning is the whole point: `contains_tax=True` and `contains_tax=False` are different quantities, and the bugs dblect targets are the ones where one is used where the other was assumed.

A field whose value has no source, neither fixed in the type nor present as a physical column, is the one error here, and it surfaces as a finding, not a crash: *"`Revenue.contains_tax` on `fct_orders.order_total` has no value: fix it in the type or map it to a column."* The framework will not guess what your revenue includes.

## Refinement: fixing fields to specific values

`Revenue` with everything open is the general type. The useful, checkable types are its **refinements**: the same fields, with some fixed to specific values.

```python
RevenueGross = Revenue.refine(contains_tax=False, contains_discount=False)
# list price x quantity, before discounts or tax (what the catalog says)

RevenueNet = Revenue.refine(contains_tax=False, contains_discount=True)
# after discounts, before tax (the usual accounting "net revenue")

RevenueCollected = Revenue.refine(contains_tax=True, contains_discount=True)
# what actually hits the bank (gross of tax, net of discount)
```

If you have used `typing.Literal` to narrow a type, this is the same move: `Revenue.refine(contains_tax=False)` is `Revenue` with `contains_tax` narrowed from "either" to exactly `False`. Refinement is partial and chainable: the three types above each leave `currency` open, so each is a family over every currency, and `RevenueNet.refine(currency=Currency.USD)` narrows one further.

`Money` refines the same way; the canonical pair the demo turns on:

```python
from dblect.types import Money, Currency

MoneyUSD = Money.refine(currency=Currency.USD)
MoneyEUR = Money.refine(currency=Currency.EUR)
```

Refinement and fixing-a-field are the same operation seen from two distances: `.refine()` names a reusable refined type, and inline `Field(...)` (below) fixes a field at one binding site. Both produce a type with fewer open fields.

## Composition: where the types earn their keep

Declaring types is setup. The return comes when SQL folds several typed values into one and the framework checks that the fold is meaningful. There is one principle, and it shows up as two everyday operations:

> When an operation combines several values of a domain type into one result, the type's meaning-bearing fields must stay coherent: anything fixed in the type must agree, and anything per-row must be held constant across what is being combined.

"Combining" happens two ways in SQL, and each is one of the scenarios that motivates this whole layer.

### Combining values across columns: tax meets no-tax

Adding a taxed revenue to an untaxed one, or unioning them, or coalescing one into the other, produces a quantity whose `contains_tax` is genuinely undefined. The type system already knows the two inputs disagree on a field fixed in the type, so it refuses the combination.

```python
class FctOrders(ModelContract):
    dbt_model = "marts.fct_orders"
    subtotal: RevenueNet                 # contains_tax=False
    tax:      TaxAmount
    total:    RevenueCollected           # contains_tax=True
```

```sql
-- models/marts/fct_orders.sql, a refactor that looks harmless
select order_id,
       subtotal + tax  as total,        -- fine: a net revenue plus its tax is a collected revenue
       subtotal + total as grand_total  -- wrong: adds an untaxed and a taxed revenue
from ...
```

```
FAIL  marts.fct_orders.grand_total [types do not combine]
      subtotal + total adds Revenue(contains_tax=False) and Revenue(contains_tax=True).
      The sum's contains_tax is undefined; these are different quantities.
      models/marts/fct_orders.sql:4
```

The rule that decides agreement is the one your intuition supplies: two refinements combine when every field they both fix agrees, and an open field meets anything.

| One side | Other side | Result |
|---|---|---|
| `RevenueNet` | `RevenueNet` | combine |
| `Revenue` (currency open) | `RevenueNet.refine(currency=USD)` | combine (open meets USD) |
| `RevenueNet` | `RevenueGross` | **conflict** (`contains_discount` disagrees) |
| `MoneyEUR` | `MoneyUSD` | **conflict** (`currency` disagrees) |

Under the hood this is a lattice meet on the substrate (see [lineage-facts.md](lineage-facts.md)); you never see the lattice, only a type that flowed down the DAG, a type you declared, and a finding where they collide. It is the same engine that already propagates nullability and uniqueness, with the domain type as one more property over it.

### Combining values across rows: the sum that quietly stops making sense

This is the scenario that shows why the record-of-fields model matters, and the one that makes the case for building on lineage rather than on annotations alone, because the model that breaks is one nobody annotated.

Start with an ordinary, untyped project. A charges source has the columns it has always had, and somewhere downstream a mart rolls them up by country. No `dblect/` declarations exist for either:

```sql
-- models/staging/stg_charges.sql      (columns: charge_id, charge_date, charge_amount, ...)

-- models/marts/revenue_by_country.sql, written long ago by someone else
select country, sum(charge_amount) as total_charges
from {{ ref('stg_charges') }}
group by country
```

With no type on `charge_amount`, `sum(charge_amount)` carries no obligation and the framework leaves it alone. The rollup is fine, and stays fine for as long as charges are single-currency.

Then a dev adds international charges. The PR grows a per-row `currency` column on the source and writes the one declaration that records what `charge_amount` now means:

```python
# dblect/contracts/staging.py    (the only thing the dev adds)
class StgCharges(ModelContract):
    dbt_model = "stg_charges"
    charge_amount: Money           # currency open: amount and currency are now a pair of columns
```

The dev has never opened `revenue_by_country.sql` and does not know it exists. The next check flags it anyway:

```
FAIL  marts.revenue_by_country.total_charges [aggregation not well-typed]
      sum(charge_amount) groups by {country}; charge_amount is Money.amount and its
      companion currency is not provably constant per group (currency was dropped in
      stg_charges -> revenue_by_country, and is per-row upstream).
      Summing Money across currencies is undefined: add currency to GROUP BY, filter to
      one currency, or convert to a common currency before summing.
      Introduced by: you declared `charge_amount: Money` over (charge_amount, currency)
                     in dblect/contracts/staging.py:StgCharges
      models/marts/revenue_by_country.sql:3
```

That is the demo's strongest beat: a single declaration on a source illuminates its entire blast radius, including a consumer the author never knew about, with cause and effect on screen, before any row of data runs.

#### How the type reaches a model with no contract

The finding lands on an undeclared model because propagation does not depend on declarations. Three moves get it there:

- **The declaration is a relational fact, not a per-column label.** `charge_amount: Money` with `currency` open binds two columns into one value: *"`charge_amount` is the `amount` field of a `Money` whose `currency` field is the column `currency`."* The companion link is part of the fact, the same way a uniqueness fact ranges over a key tuple rather than a single column.
- **The fact rides column-level lineage across the whole DAG.** Domain type is one more property over the substrate ([column-level-lineage.md](column-level-lineage.md)), the engine that already moves nullability and uniqueness cross-model. It propagates through every model that selects `charge_amount`, contract or no contract, so the downstream reference arrives carrying "I am `Money.amount`, and my companion is the `currency` that traveled with me."
- **The aggregation check is conservative.** To *permit* `sum(charge_amount) group by country`, the framework must *prove* the companion `currency` is constant within each group: present in the `GROUP BY`, fixed in the type, narrowed by a `WHERE currency = ...`, or fixed by a declared functional dependency `country -> currency`. Absent a proof, it flags. It fires here not because dblect knows the currencies are mixed, but because every path to proving they are not is closed: `currency` was projected away before the rollup, and upstream it is per-row.

The general rule: an arithmetic reduction (`sum`, `avg`, and friends) over one field of a multi-field domain type is well-typed only when the type's other fields are provably constant across the reduced set. The framework reads the grouping keys from the query and checks them against the companion fields; the machinery lives with the substrate (see [propagation-soundness.md](propagation-soundness.md)).

#### Telling dblect the sum is fine

Conservatism cuts both ways, so the ways to discharge an obligation are part of the contract, not afterthoughts. There are three, and each says something true about the data. The author reaches for whichever matches reality.

**Fix the SQL: group by the tag.** Adding `currency` to the grouping makes it constant per group by construction, and the result is honestly per-currency:

```sql
select country, currency, sum(charge_amount) as total_charges
from {{ ref('stg_charges') }}
group by country, currency
```

**Assert a functional dependency, when one genuinely holds.** If each country bills in exactly one currency, the group key already determines the currency, and you can keep summing by country alone. You state that fact on the contract for the relation where it holds, as a symbolic expression over column proxies, the same shape the `ModelContract` contract methods use (the conservation method shown later under [ModelContract](#modelcontract-binding-types-to-a-models-columns)). The same `StgCharges` contract is spelled out in full here, with `country` and `currency` declared and `charge_amount` bound explicitly, so the dependency has named proxies to range over:

```python
class StgCharges(ModelContract):
    dbt_model = "stg_charges"

    country:       Country
    currency:      Currency
    charge_amount: Money.columns(amount="charge_amount", currency="currency")

    @contract.functional_dependency
    def country_sets_currency(self):
        return self.country.determines(self.currency)   # each country uses one currency
```

With that, `sum(charge_amount) group by country` checks clean, because `country -> currency` lets the framework conclude the currency is single-valued in each country group even though the `currency` column was projected away before the rollup. The result keeps its tag: `total_charges` is a `Money` whose currency is now determined by `country`, so a later `sum` across countries lights up again on its own. A functional dependency buys one sound aggregation, not a blanket exemption. And because it is a checkable claim rather than a bare assertion, the runtime check verifies it against data; a country that turns out to bill in two currencies becomes its own finding rather than silently licensing the mix.

**Let the join speak for itself.** When `currency` arrives by a lookup against a dimension keyed on `country`, the dependency is structural and the framework infers it, with no declaration at all, the way an existing `relationships` test is already read as a foreign key:

```sql
select c.country, sum(c.charge_amount) as total_charges
from {{ ref('stg_charges') }} c
join {{ ref('dim_country') }} d using (country)   -- d.currency is a function of country
group by c.country
```

A single-currency mart that filters `where currency = 'USD'` discharges the obligation the same way, by fixing the tag. These recognizers are what keep a sound-by-default check from crying wolf, and the full set of discharge paths and their grounding lives in [domain-type-algebra.md](domain-type-algebra.md).

### Joining values: the total that gets counted twice

A join pairs rows, so it does not combine magnitudes the way `+` or `sum` do, but it changes how many times each magnitude appears, and that is its own class of finding. The classic case is the fan-out, where a one-row-per-order total is replicated by a join to a many-rows-per-order child and then summed.

```python
class FctOrders(ModelContract):
    dbt_model = "marts.fct_orders"
    order_id:    PrimaryKey
    order_total: Money(currency=Currency.USD)

class StgOrderItems(ModelContract):
    dbt_model = "stg_order_items"
    order_item_id: PrimaryKey
    order_id:      ForeignKey("marts.fct_orders.order_id")   # many items per order
    quantity:      Count
```

```sql
-- models/marts/order_revenue.sql, looks like an innocent rollup
select o.order_id, sum(o.order_total) as revenue
from {{ ref('fct_orders') }} o
join {{ ref('stg_order_items') }} i using (order_id)   -- one row per item, not per order
group by o.order_id
```

The join replicates each `order_total` once per line item, so `sum(o.order_total)` counts a three-item order's total three times. The currencies all agree, so this is not a tag conflict; it is a grain violation, and the framework catches it because it knows `fct_orders.order_id` is the key of the order total's origin and that the join did not preserve it:

```
FAIL  marts.order_revenue.revenue [aggregation over a fanned-out magnitude]
      sum(order_total) sums a Money whose origin key fct_orders.order_id is not
      preserved through the join to stg_order_items (one row per item, many per order).
      Each order_total is replicated per line item and counted more than once.
      Sum at the order grain before joining, or aggregate a measure native to the
      item grain (such as quantity) instead.
      models/marts/order_revenue.sql:2
```

The declared `ForeignKey` is what makes the grain explicit: it tells the framework that `stg_order_items` is the many side, so the join fans the order out. An existing dbt `relationships` test is read the same way, so a project that already tests its keys gets this finding without new declarations. The companion check at the join's `ON` clause is type compatibility: joining on two columns whose types do not unify, an ISO-2 `Country` against an ISO-3 one, or a `MoneyUSD` amount against a `MoneyEUR` one, is itself a finding, because a join predicate is a comparison and a comparison requires the tags to agree. The full treatment of grain alongside tag coherence is in [domain-type-algebra.md](domain-type-algebra.md).

## ModelContract: binding types to a model's columns

A `ModelContract` binds domain types to one dbt model's columns and is the unit a reader opens to ask "what is this model supposed to be?"

```python
# dblect/contracts/marts.py
import dblect
from dblect import ModelContract, contract, models
from dblect.types import Date
from ..types import RevenueNet, TaxAmount, OrderId

class FctOrders(ModelContract):
    """One row per order, with order-level totals."""

    dbt_model = "marts.fct_orders"

    # field name == SQL column name, for the common single-column case
    order_id:    OrderId
    customer_id: dblect.ForeignKey("dim_customers.customer_id")
    order_date:  Date
    order_total: RevenueNet = dblect.Field(ge=0)
    tax_paid:    TaxAmount  = dblect.Field(ge=0)

    @contract.conservation(tolerance=0.01)
    def total_matches_line_items(self):
        """Order header total reconciles to the sum of line-item subtotals."""
        return (
            self.order_total.sum().group_by(self.order_id)
            == models.stg_order_items.subtotal.sum()
                 .group_by(models.stg_order_items.order_id)
        )
```

The moving parts, each keeping its Pydantic or dbt instinct:

- **`dbt_model = "marts.fct_orders"`** binds the class to a manifest entity, resolved with the rules dbt uses for `{{ ref() }}`: bare names resolve locally then in packages, ambiguous ones demand qualification.
- **Field name equals column name** in the common case. `order_total: RevenueNet` says the model's `order_total` column has type `RevenueNet`. Because `RevenueNet` has exactly one open field (`amount`), that field maps to the `order_total` column and the reading is the plain Pydantic one.
- **`dblect.Field(...)`** carries column-level metadata, the same role as Pydantic's `Field(...)`. Its two jobs are below.
- **`dblect.ForeignKey("dim_customers.customer_id")`** is a parameterized type naming another model's column. It doubles as the edge the fixture builder uses to coordinate multi-table generation. An existing dbt `relationships` test is read as a foreign key for free, so you do not restate it.
- **Contract methods** decorate functions that build symbolic expressions over column proxies (`self.order_total`, `models.stg_order_items.subtotal`). They are runtime-checkable invariants, covered in the [technical intro](dblect_technical_intro.md). A contract with only column declarations and no methods is valid and already buys type propagation.

### When a type spans more than one column

A field typed with a multi-open-field type maps onto more than one physical column. The default is a name convention, with an explicit override when the warehouse disagrees:

```python
class StgSales(ModelContract):
    dbt_model = "stg_sales"

    # convention: an open field f of Money maps to column {contract_field}_{f}
    sale: Money                                   # -> columns sale_amount, sale_currency

    # explicit, when the columns are not named by convention
    refund: Money.columns(amount="refund_value", currency="refund_ccy")
```

The convention degrades to the scalar case cleanly: with exactly one open field, the column is just the contract field name (`order_total`, not `order_total_amount`). This is the same flattening every nested-record-to-table mapper performs, so the instinct carries; the open question at the end is whether the convention should be `{field}_{subfield}` or something the framework infers from the manifest.

### Registration and resolution: a typo is a finding, not a crash

Classes register on definition through `__init_subclass__`, the import-time discovery pytest and Pydantic use. The framework scans `dblect/`, imports every module, and every `ModelContract` and `DomainType` lands in a registry. Resolution against the manifest (does `marts.fct_orders` exist? does `dim_customers.customer_id`?) runs *after* the whole scan completes, so a misspelled `dbt_model` or a renamed column surfaces as a finding in the report alongside the others, rather than as an `ImportError` that blinds the analyzer to the rest of the project. One broken contract file does not take down the audit.

### The editor experience: `models` and generated stubs

Contract bodies reference other models through `models.stg_order_items.subtotal`. By default `models` is a lazy proxy: `__getattr__` all the way down, capturing symbolic references the framework validates later, with zero setup and zero codegen. For the full editor experience, `dblect init` reads the manifest and writes `dblect/_stubs/models.py` with a concrete class per dbt model; you `from dblect._stubs import models` and get autocomplete, type-checking, and refactor-rename across contracts. The stubs regenerate when the manifest changes. This is the Prisma and dlt generated-client pattern: the generated file lives in its own package, is gitignored, and is never hand-edited. The editor experience is the reason this surface is Python, so it gets first-class treatment.

## `Field`: constraints and inline fixing

`dblect.Field(...)` does two jobs, and seeing the split keeps the trust model honest.

```python
order_total: RevenueNet = dblect.Field(gt=0)                     # a constraint
discounted:  Revenue    = dblect.Field(contains_tax=False,       # inline fixing
                                       contains_discount=True)
```

- **Constraints** like `gt=0` are *checkable* claims about the column's values. `dblect.Field` accepts Pydantic's constraint vocabulary directly (`gt`, `ge`, `lt`, `le`, `multiple_of`, `min_length`, and the rest), so the muscle memory transfers, with a few readable aliases on top (`non_negative=True` for `ge=0`). The framework can prove or refute them against generated or real data, and trusts them the way it trusts anything it can verify.
- **Inline fixing** like `contains_tax=False` fixes a field right at the binding site, exactly equivalent to annotating the column with `RevenueNet`. It is a *vouched* meaning: a thing you assert about what the column means, which the framework propagates and reconciles but cannot independently prove from the SQL.

Both ride one `Field` surface because that matches the Pydantic instinct, and the framework tags them by trust class internally (checkable constraint versus asserted meaning). Prefer a named refined type (`RevenueNet`) when the meaning recurs; reach for inline `Field(...)` for the one-off.

## Flags: a logical column whose value a dbt var selects

A dbt `var()` changes what your models produce, and when it gates a branch that changes a column's meaning, it fixes a field to one value in one configuration world and another in the next. This is the logical column at its most dynamic: the value still comes from your declarations rather than the data, but which value depends on the build. Flags are declarations too, and they look like every other class here. The full surface, discovery, and world enumeration live in [flags_and_configs_as_types.md](flags_and_configs_as_types.md); this is just enough to place them in the authoring story.

```python
# dblect/flags.py
import dblect
from dblect import DomainFlag, RefinementEffect
from .types import Revenue

class IncludeTaxInRevenue(DomainFlag):
    """When set, revenue values include sales tax."""
    dbt_var = "include_tax_in_revenue"
    type    = bool
    default = False
    affects = RefinementEffect(
        target=Revenue.contains_tax,
        value_when_true=True,
        value_when_false=False,
    )
```

A flag carries its link to the dbt var, its type and domain, its default, and an `affects` clause naming which field on which type it fixes. The flag knowing the type is what lets one flag target several fields or several types and keeps all flag effects in one registry. `dblect init` scaffolds draft flag classes from the vars it finds in your SQL, pre-filling everything it can infer and leaving the `affects` clause for you, since the meaning of the flag is the one thing the framework cannot read off the Jinja.

With that declaration, a column whose SQL branches on the var has a type per flag world, and the framework checks every world:

```
flag-world analysis for marts/discounts.sql

  world: include_tax_in_revenue=False ... PASS
  world: include_tax_in_revenue=True  ... FAIL
        revenue declared RevenueNet (contains_tax=False)
        inferred Revenue(contains_tax=True) under this world
```

That is the configuration-space catch: a bug latent in a flag world nobody has flipped yet, surfaced before it ships.

## A complete `dblect/` tree

The authored surface assembled, the parts this document covered shown together:

```python
# dblect/types.py
import dblect
from dblect.types import Decimal, Currency

class Revenue(dblect.DomainType):
    """Revenue, with what it includes and its currency recorded as fields."""
    amount:            Decimal(18, 2)
    contains_tax:      bool
    contains_discount: bool
    currency:          Currency

RevenueGross = Revenue.refine(contains_tax=False, contains_discount=False)
RevenueNet   = Revenue.refine(contains_tax=False, contains_discount=True)
```

```python
# dblect/contracts/staging.py
from dblect import ModelContract, Field, ForeignKey
import dblect.types as t
from ..types import RevenueNet

class StgPayments(ModelContract):
    dbt_model = "stg_payments"

    payment_id:     t.PrimaryKey
    order_id:       ForeignKey("stg_orders.order_id")
    payment_method: t.Varchar
    amount:         RevenueNet = Field(ge=0)
```

```python
# dblect/flags.py
from dblect import DomainFlag, RefinementEffect
from .types import Revenue

class IncludeTaxInRevenue(DomainFlag):
    """When set, revenue values include sales tax."""
    dbt_var = "include_tax_in_revenue"
    type    = bool
    default = False
    affects = RefinementEffect(
        target=Revenue.contains_tax, value_when_true=True, value_when_false=False,
    )
```

```
my_dbt_project/
├── models/                 # your dbt models, untouched
└── dblect/
    ├── __init__.py
    ├── types.py            # DomainType definitions and refinements
    ├── flags.py            # DomainFlag declarations
    ├── contracts/
    │   ├── staging.py      # ModelContract per staging model
    │   └── marts.py        # ModelContract per mart
    └── _stubs/
        └── models.py       # autogenerated, gitignored
```

The directory sits beside dbt's, never intrudes on it, and is fully optional: a project with no `dblect/` is a zero-declaration audit candidate that still gets the structural findings.

## The Pydantic-to-dblect cheat sheet

For the reader placing this against what they already know:

| Pydantic | dblect | What is the same | What differs |
|---|---|---|---|
| `class X(BaseModel)` | `class X(DomainType)` / `class X(ModelContract)` | class-as-declaration, annotated fields | own metaclass, never instantiated to validate a row |
| a field holds one row's value | a `DomainType` field is a physical column or a logical column | the field/record shape | a field can be physical (from the data) here and logical (from the type) there; `currency` is the example |
| a field holds one row's value | a `ModelContract` field names a SQL column (or several) | annotation syntax, field naming | the field name is the column name; a multi-field type spans several columns |
| `Field(gt=0)` | `dblect.Field(gt=0)` | the same constraint vocabulary (`gt`/`ge`/`lt`/`le`/...), plus aliases like `non_negative=True` | `Field` also fixes a field inline (a vouched meaning) |
| `Annotated[int, Gt(0)]` | `Annotated[Decimal, Gt(0)]` | the `Annotated` constraint idiom | constraints are checked against data, not on assignment |
| `Literal["a", "b"]` narrowing | `T.refine(field=value)` | narrowing a type to a specific case | narrows by fixing a field; partial and chainable |
| `model_config` class attribute | `dbt_model = "..."` class attribute | class-level config attribute | binds to a dbt manifest entity |
| `@field_validator` | `@contract.conservation(...)` etc. | decorated methods on the class | builds a symbolic expression AST, checked statically or by PBT |
| generated client (Prisma/dlt) | `dblect/_stubs/models.py` | regenerate-on-schema-change typed client | generated from the dbt manifest |

The one row to internalize is the second: every field is a column, and its value comes from the data (physical) or the type (logical). Every collision the framework reports is a consequence of that and of keeping the meaning-bearing fields coherent when SQL folds values together.

## Open questions

The genuinely unsettled parts of the authoring surface. None blocks a working first version; each is best settled when a real declaration forces it.

- **Multi-column binding convention.** When a multi-field type spans columns, the default mapping (shown here as `{contract_field}_{field}`) needs to be settled, along with how aggressively `.columns(...)` overrides interact with the generated stubs and with dbt `relationships` tests already present. The single-open-field case (column == contract field name) is settled; the multi-field case is where a real schema should decide the convention.
- **Functional-dependency surface.** Discharging an aggregation with `country -> currency` is shown here as a `@contract.functional_dependency` method returning `self.country.determines(self.currency)`. The operator spelling (`determines(...)` versus a `>>` sugar), whether a dependency can be declared across models rather than only on the relation where it holds, and how far the substrate propagates a declared dependency through joins and unions before it must be restated, all want a real multi-currency project to settle. The semantics and the three discharge paths are fixed in [domain-type-algebra.md](domain-type-algebra.md); only the authoring spelling is open.
- **How visible the trust split in `Field` should be.** `Field(ge=0)` (a checkable constraint) and `Field(contains_tax=False)` (a vouched value) ride one surface. Whether the author should see the trust distinction (a separate keyword or call) or have it stay internal is open. One surface matches the Pydantic instinct; a visible split matches the framework's own provenance model.
- **Detecting the unsound aggregate versus the unsound assignment.** When `sum(amount)` mixes currencies and the result is also assigned to a `MoneyUSD` column, there are two true statements: the aggregation is not well-typed, and the declared output type is wrong. Whether to report one finding or two, and which to make primary, is a diagnostics call that wants real output in front of real users before it is fixed. The same question applies to the cascade in the currency-creep scenario.
- **Call-syntax sugar for refinement.** `Money(currency=Currency.USD)` reads as shorthand for `Money.refine(currency=Currency.USD)` and is used throughout this doc for fixing a field at use. `.refine()` is canonical for naming a reusable type; whether the call form is exactly equivalent sugar or reserved for inline use is a small consistency call.
- **String literals on enum fields.** Accepting `currency="USD"` and validating against the `Currency` enum is friendlier at the call site; requiring `Currency.USD` keeps the surface free of stringly-typed values. A reasonable resolution accepts both and treats an out-of-domain literal as a finding, but the default the docs should teach is unsettled.
- **Eager versus lazy registration.** Import-time `__init_subclass__` registration is simple and matches Pydantic and Pandera. For projects with hundreds of contracts a lazy `dblect.scan(path)` may be warranted. The crossover point is unknown until a large real project exists to measure.

The deeper theory under the aggregation rule (why magnitudes and tags are inferred from field algebra rather than annotated, which reductions are well-typed over which fields, how unit conversion fits, summability and coherence in general) is worked out in [domain-type-algebra.md](domain-type-algebra.md). The authoring surface here rests on it but stays small because of it: the author declares ordinary typed fields, and the magnitude/tag classification and the composition rules fall out of the types' algebra.
