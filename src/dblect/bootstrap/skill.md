# Bootstrap dblect types and contracts for a dbt project

Read a dbt project, work out which columns carry meaning plain validation cannot
see, draft the dblect declaration layer that pins it, then run `dblect check` and
correct the draft until every declaration lines up and every finding is explained.

dblect already earns its keep with zero declarations: its structural detectors read
the compiled SQL and flag ordering, join, and NULL hazards on their own, and it
reads your existing dbt tests directly. A `unique` (or
`dbt_utils.unique_combination_of_columns`) test becomes a key fact, a
`relationships` test a foreign-key edge, an `accepted_values` test a value domain.
Re-declaring a fact a dbt test already states adds noise and no signal.

**But check that the tests exist before relying on them.** Projects often test their
models thoroughly and their sources not at all, so the raw tables every join starts
from carry no key dblect can see, and its join and grain checks go quiet exactly where
the data is messiest. The facts are usually written down anyway, in prose: a source
description saying "one row per booking", a comment saying a seat number "is unique
only within its flight", a doc listing the values a status column takes. Step 2 finds
those gaps and fills them.

What no dbt test can express, and what you add, is *meaning*: a column whose type is
richer than its SQL type, so two numerically valid values still are not
interchangeable. dblect propagates that meaning along the DAG and reports where it
shifts before it reaches a dashboard. Money is the running example (a revenue figure
sliding from net to gross, a currency creeping in), but it is one instance of a
general idea.

## What is in scope

Three kinds of meaning propagate today:

- **A magnitude carrying a unit.** A summable quantity plus the tag saying what it is
  measured in, so values in different units stop being interchangeable. Money in a
  currency is canonical; a physical quantity in a unit, or a rate, is the same shape.
  Expressed as a `DomainType` (a magnitude facet plus a `UnitEnum` or `NominalEnum`
  tag), with **refinements** fixing a meaning-bearing parameter (single currency, net
  vs gross, tax inclusive). This catches a unit changing upstream
  (`domain_type_contradiction`) and a mixed-unit sum (`aggregation_not_well_typed`).
- **An identifier naming its entity.** A key column tagged with what it identifies,
  so a join that equates an order id with a customer id stops looking like a clean
  one-to-one join. Expressed as a `DomainType` with no magnitude: one open value
  field (the id) plus a `NominalEnum` entity facet, refined per entity. This catches
  a join across entities (`join_key_type_mismatch`).
- **A structural invariant over columns.** A functional dependency (an
  `ad -> adset -> campaign` hierarchy, or every payment on an order sharing the
  order's currency) or the grain, which keep a rollup well typed. Expressed as
  `determines` and `grain` facts, **not** as types.

Closed categories (`status`, `channel`, `platform`, `country`) need no declaration
where an `accepted_values` test exists: the test already grounds and propagates a
value domain, so a stray or mistyped literal downstream (`WHERE status = 'shipd'`)
is caught without you writing anything. Where no test exists, a bare
`NominalEnum`/`UnitEnum` column declaration grounds the same fact (Step 2). A category
earns a full `DomainType` only when it rides on a magnitude as its unit, as
`currency` does on `Money`.

Restraint is part of the job. Type a column only when one of those kinds
genuinely lives in it. Give an identifier an entity type when it is a join key and
the project has several ids of different entities that could be confused; leave an
id that is never joined, a free-text column, or a one-off numeric column as its SQL
type. Uniqueness needs no type: a key is already read from its dbt test. Skip a
**data-dependent unit** too, where the parameter fixing a magnitude's meaning lives
in the data rather than in code or a companion column: a "season wins" figure whose
basis is 80 games some seasons and 82 others has no column to bind to, and pinning it
would vouch a claim that is wrong half the time. A vouched declaration the data does
not support is worse than none, because dblect trusts it and propagates it. When in
doubt, leave it or ask (Step 4).

Out of scope this pass, because they do not run on the `dblect check` path yet:
configuration flags (`DomainFlag`) and runnable contract predicates (`.within(...)`).
Stick to domain types, refinements, and fact-returning contracts.

## Step 1: orient

Confirm how dblect is invoked: run `dblect --help`. If it is not on PATH it may live
in `.venv/bin/dblect` or run through `uv run dblect` / `poetry run dblect`. Settle
this once; do not search the filesystem for the package.

Confirm a `dbt_project.yml` exists and dblect has a manifest. If no manifest exists
yet, `dblect init` produces one (it falls back to `dbt compile`):

```text
dblect init .
```

`init` scaffolds the `dblect/` tree and writes `dblect/_stubs/models.py`. dblect finds
the manifest and catalog under the project's dbt target-path on its own, so a
non-default `target-path` needs no extra flags.

**Note the Python models, because dblect cannot read them.** dblect parses compiled
SQL, so a dbt Python model is reported under `skipped:`. This matters downstream: a
column whose lineage passes through a skipped model loses its provenance, which can
turn a real check into a conservative artifact (Step 6). Note them up front so you
recognize the gap.

**Get column names right; every binding depends on them.** Two sources feed
resolution and differ in coverage. `schema.yml` lists only documented columns, so the
generated stubs can be blind to an undocumented `stg_payments.amount` or carry a name
stale against the SQL: treat them as a hint. `catalog.json` is the warehouse's
account of every column dbt emitted, the ground truth, read when it sits beside the
manifest. Produce it before relying on resolution: `dbt docs generate` writes it (or
`dbt build` first if the models are not built). Without it, undocumented leaf columns
(seeds, sources) will not resolve and bindings look missing when they are merely
unseen. When stubs look thin, read a model's real columns from `catalog.json`
(`nodes.<unique_id>.columns`).

Then read the structure: the `models/` tree, each `.sql`, and the `schema.yml`
descriptions, which are often where a human already wrote down what a column means.

## Step 2: fill the gaps in what dblect already knows

List the relations the project's joins and window rankings read, sources first, and
for each note which key and foreign-key tests dblect can see. Wherever a relation that
is joined on or ranked has none, find its key: the source description, comments in
the SQL, docs and runbooks in the repo, a `DISTINCT` or `ROW_NUMBER` dedup that says
what the author believed was unique. Do the same for closed sets of values: a status,
type or kind column that the SQL filters on with literals (`where status = '...'`)
and that has no `accepted_values` test.

**Verify against the data before you vouch.** When the warehouse is reachable, run the
check a dbt test would (`select k, count(*) from t group by k having count(*) > 1`, or
`select distinct status from t`). A key that holds is a fact; one that fails is a
finding to report, not to declare. When you cannot query, declare only what the
project's own text states outright, and say which facts rest on prose alone.

Declare each fact on the earliest relation that carries it, which for a raw column is
the source itself (`dbt_model` takes `"<source_name>.<table>"`). Facts flow downstream
and never upstream, so a set declared on a staging model is invisible to anything
that reads the raw table directly, such as orchestrator tasks, scripts and notebooks.
A composite key is one `key(...)` over all its columns, and it matters: a column
unique only together with another (a seat number within a flight) joined or grouped
on alone is a classic silent bug. A model whose docs say "one row per X" gets
`key(X)` when no test states it, intermediate models included, so a join that breaks
the grain is caught where it happens.

```python
from dblect import ModelContract, contract, models
from dblect.types import NominalEnum

class ShipmentStatus(NominalEnum):
    PENDING = "pending"
    SHIPPED = "shipped"
    DELIVERED = "delivered"

class RawShipments(ModelContract):
    dbt_model = "raw.shipments"
    status: ShipmentStatus  # a closed value set: a filter on any other literal is dead

    @contract
    def pk(self):
        return self.key(self.shipment_id)

class RawShipmentLines(ModelContract):
    dbt_model = "raw.shipment_lines"

    @contract
    def pk(self):
        return self.key(self.shipment_id, self.line_number)

    @contract
    def parent(self):
        return self.shipment_id.references(models.shipments.shipment_id)
```

Mention each of these to the user as a dbt test worth adding too: the test also guards
the data at run time, which a declaration does not. The converse holds as well. A
declaration never fails a load, so a project's rules about which runtime tests to keep
(for instance, no `accepted_values` on staging, since a new status is not a defect) do
not govern it. When a new value appears, adding it to the declared set is the intended
step.

**Ids from different systems.** Where two systems each mint ids for the same kind of
thing (a CRM's account id and the billing system's, a vendor's product code and the
catalog's SKU), the columns share a name or a SQL type and nothing else.
Give each an identifier entity type (Step 5) so a join that equates them is caught.

This step fills in keys and value sets; the meaning pass in Steps 3 to 5 still runs
in full after it. A project needs both.

## Step 3: find the loaded columns

Walk models and macros for columns whose meaning is richer than their SQL type:

- **Magnitudes and their units.** Any `amount`, `price`, `revenue`, `cost`, `spend`,
  `total`, `value` column and the unit qualifying it: currency, net vs gross, tax in
  or out, pre- or post-discount. A `Decimal` records none of this. Quantities carry
  units too (`qty` with a `uom`, a duration in seconds or days, a weight in grams or
  pounds). Rates (`cpc`, `ctr`, `rate`, `pct`) are magnitudes as well: ask the base and
  the window.
- **Hierarchies and grain.** A containment chain (`ad` within `adset` within
  `campaign`, `order_line` within `order`) where one identifier determines another,
  and the grain itself when a rollup depends on it. Skip the plain keys a dbt test
  already declares; the value is the functional dependency, not the keys.

Read the SQL to form a hypothesis: `sum(amount)` grouped by `order_id` says the
author believes an order's payments are summable; a conversion macro says money
crosses currencies; `group by campaign_id` over an ad-grained model relies on each ad
belonging to one campaign. Trace a loaded column from source through staging to
marts, watching for where its meaning could change without the type following.

Declare a magnitude's type once, on the model where it first meets a complete unit,
and let dblect carry it downstream. Where a raw unit column is sometimes NULL and a
later model fills it in (a `coalesce` to the market's currency, say), bind there, and
report the NULL rows to the user as a finding of their own. Do not re-declare the type
on models that pass the column through: the type already flows in, and a second
binding to the downstream copy of the unit column reads as a contradiction.

## Step 4: infer what you can, interview for the rest

Some meaning is readable: `where currency = 'USD'` is single-currency by
construction, a macro named `to_usd` converts. Lean on the SQL, the descriptions, and
dbt test metadata.

The rest you should not guess: is this `amount` still one currency, or did the source
go multi-currency? Is `revenue` net or gross? Is the grain one row per order or per
order line? **Interview the user**: propose your reading and ask before writing a
vouched fact ("I read `stg_payments.amount` as USD because the model filters to US
orders; still true now that the source carries a `currency` column?"). If you cannot
ask, draft it and mark the assumption with `# TODO: confirm ...`.

## Step 5: write the declarations

Put domain types in `dblect/types.py` and contracts under `dblect/contracts/`. Copy
the shapes below rather than reverse-engineering dblect's source. Every `.py` module
under `dblect/` is imported and any `ModelContract` subclass registers itself, so you
wire up no imports or registry; split one module per model group or per model as you
like. A contract module imports the project's own types relatively
(`from ..types import Money`), because `dblect.types` names the library's module.
Run any Python probe from outside the project's `dblect/` folder and its parent:
from there, the folder shadows the library on import, and its `types.py` shadows
Python's own `types` module.

Declare a domain type by subclassing `DomainType`, one typed field per facet. `Money`
(an amount and its currency) ships in `dblect.demo`:

```python
from dblect.types import Decimal, DomainType, UnitEnum

class Currency(UnitEnum):
    USD = "USD"
    EUR = "EUR"

class Money(DomainType):
    amount: Decimal(18, 2)
    currency: Currency
```

A `UnitEnum` tag must agree when values combine; a `NominalEnum` tag rides along
without that constraint. The magnitude must be a `Decimal`: dblect does not read an integer
field as a magnitude, so integer minor units (cents) are `Decimal(38, 0)`.

A quantity in a unit of measure is the same shape:

```python
from dblect.types import Decimal, DomainType, UnitEnum

class Uom(UnitEnum):
    EACH = "each"
    KG = "kg"

class Quantity(DomainType):
    qty: Decimal(18, 3)
    uom: Uom
```

**Refine to fix a meaning-bearing parameter.** Pin a single-currency column's tag;
bind a multi-currency column's facet to the column recording it:

```python
from dblect.demo import Currency, Money

# Single currency: pin the tag, so a mixed-currency sum stops being well typed.
RevenueUSD = Money.refine(currency=Currency.USD)

# Multi-currency: bind the currency facet to the column that records it.
PaymentMoney = Money.columns(amount="amount", currency="currency")
```

**A fixed-scale magnitude is the same shape with a one-member unit.** An Elo rating, a
probability on a `0..10000` scale, a score: not plain `Decimal`s, since an Elo and a
probability are not interchangeable though both are numbers. Give the magnitude its
own one-member `UnitEnum` and `refine` to pin it (pinning is what sources the unit
facet; leave it open and the facet has no column behind it, which surfaces as a
contract issue). For a dimensionless count, use the built-in `Count`, always safe to
sum.

```python
from dblect.types import Decimal, DomainType, UnitEnum

class RatingScale(UnitEnum):
    ELO = "elo"

class Rating(DomainType):
    value: Decimal(10, 2)
    scale: RatingScale

EloRating = Rating.refine(scale=RatingScale.ELO)
```

**Bind types with a `ModelContract`, and know the binding rule.** A contract names its
model with `dbt_model` and annotates each typed column; bindings alone, with no
methods, already buy propagation. The one mechanic to get right: a domain type binds
its *magnitude facet* (for `Money`, the field `amount`) to the warehouse column of the
*same name*, not to the contract field on the left. So several money columns not named
`amount`, each annotated with a bare type, all point at one phantom `amount`,
collapse, and resolve to nothing. Map each with `.columns(...)`:

```python
from dblect import ModelContract
from dblect.demo import Currency, Money

MoneyUSD = Money.refine(currency=Currency.USD)

class Orders(ModelContract):
    dbt_model = "orders"
    amount: MoneyUSD  # named `amount`, so the bare refinement binds it
    credit_card_amount: MoneyUSD.columns(amount="credit_card_amount")
    coupon_amount: MoneyUSD.columns(amount="coupon_amount")
    bank_transfer_amount: MoneyUSD.columns(amount="bank_transfer_amount")
```

Bare type only when the column is named after the facet; otherwise
`.columns(amount="real_column")`.

**An identifier is a type with no magnitude.** Its one open field is the column, and
the facets you pin with `refine` become the column's tag. The binding rule is the
same: `id` binds to a column named `id` unless `.columns(id=...)` says otherwise.
Pin the entity on every use. A type left with no pinned facet, or with more than one
open field, is a `contract_issue (malformed_declaration)`.

```python
from dblect import ModelContract
from dblect.types import DomainType, Integer, NominalEnum

class Entity(NominalEnum):
    CUSTOMER = "customer"
    ORDER = "order"

class EntityId(DomainType):
    id: Integer
    entity: Entity

CustomerId = EntityId.refine(entity=Entity.CUSTOMER)
OrderId = EntityId.refine(entity=Entity.ORDER)

class StgOrders(ModelContract):
    dbt_model = "stg_orders"
    order_id: OrderId.columns(id="order_id")
    customer_id: CustomerId.columns(id="customer_id")  # a foreign key keeps its parent's entity
```

**Add a fact when a rollup needs it.** A `@contract` method returning a fact
discharges an obligation the analyzer cannot see on its own. The vocabulary is small
and structural: `determines` (a functional dependency), `key`, `references`, `grain`.
The canonical case: every payment on an order shares the order's currency, so the
per-order sum is well defined. A containment hierarchy is the same `determines` fact
applied per step (`ad_id.determines(adset_id)`, then
`adset_id.determines(campaign_id)`).

```python
from dblect import ModelContract, contract
from dblect.demo import Money

class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    value: Money.columns(amount="amount", currency="currency")

    @contract
    def one_currency_per_order(self):
        return self.order_id.determines(self.currency)
```

## Step 6: check and self-correct

```text
dblect check .
```

**Read the coverage line before the findings.** The key counter is how many contract
columns resolved against the compiled SQL. If it is below the number you declared,
some bindings did not land even with no finding firing: usually the binding-rule
collapse above, or a missing `catalog.json`. A grounding count like `domain_type 7/27`
is expected and right (you type only the columns that carry meaning). Chase the
resolved-columns count up to what you declared; do not chase grounding up to the
total.

Then read the findings. Four kinds matter:

- **`contract_issue`**: a declaration does not line up with the manifest. The head
  names the precise cause in parentheses, e.g. `contract_issue (unsourced_field)`, and
  the message names the field. The causes are `unresolved_model` (misspelled or
  renamed), `ambiguous_model`, `unknown_column` (not on the model), `unsourced_field`
  (a type facet with no column behind it), `out_of_domain_value`, and the declaration
  and foreign-key variants. Let the parenthesized cause tell you which fix applies,
  then correct the declaration against the real names in `_stubs/models.py`.
- **`domain_type_contradiction`**: the meaning you declared conflicts with what the
  DAG carries (a currency creeping in where you pinned USD, a net figure flowing into
  a gross contract). The headline catch. Decide whether the declaration is stale (open
  it up) or the SQL has a real bug (fix it). One cause is your own: a type re-declared
  on a model that passes the column through. Delete the downstream declaration.
- **`aggregation_not_well_typed`**: an aggregate combined values the analyzer could
  not prove are the same kind. Often the real catch (a mixed-currency sum), but it can
  be a conservative artifact when the group shape is unresolved or the lineage routes
  through a skipped Python model (Step 1). Test before acting: does it fire
  independent of the column it supposedly conflicts with, and does the lineage trace
  through a skipped model or unresolved group? If so it is a can't-prove artifact from
  a lineage gap; note it and move on rather than contorting declarations to chase it.

- **`join_key_type_mismatch`**: a join equates two columns whose declared types
  conflict, most often ids of two different entities. This is nearly always a real
  bug in the join condition; show it to the user.

Run the check once per batch of edits and save its output to a file, so you read it
rather than rerun it. You are done when no contract issue remains, the resolved-columns
count matches what you declared, and every remaining contradiction is explained. A
remaining `domain_type_contradiction` may be a true finding worth surfacing to the
user; explain it and let them decide. List the can't-prove aggregation findings for the
user as unproven, with the reason (a computed group key such as `coalesce(currency,
...)`, a skipped model), and leave the declarations as they are. Report how many
findings stand; do not describe a run with open findings as clean.

**Suppressing a finding you and the user agree is intentional.** dblect reads
SQLFluff-compatible `-- noqa` comments, the same syntax dbt Fusion's `dbt lint`
honors, so one comment can speak to both tools. A bare `-- noqa` on the finding's line
(or the line just above it) silences every dblect finding there. To silence one
detector, name its code: `-- noqa: DBLECT_<KIND>`, where `<KIND>` is the finding kind
uppercased (so `aggregation_not_well_typed` becomes `DBLECT_AGGREGATION_NOT_WELL_TYPED`,
`join_fanout` becomes `DBLECT_JOIN_FANOUT`). Codes that do not start with `DBLECT_` are
lint rule codes that belong to `dbt lint`, so a directive like
`-- noqa: RF01, DBLECT_JOIN_FANOUT` quiets the lint rule and our finding at once.
Every suppression is still listed in the report's `suppressed:` section, so a silenced
finding stays visible in review rather than vanishing. Suppress only what the user has
confirmed is intended; never reach for `-- noqa` to make a contradiction you have not
explained go away.

## What good looks like

A small, honest declaration layer: the keys, foreign keys and closed value sets the
project documents but does not test, each checked against the data; domain types on
the magnitudes that carry a unit, refinements that pin the unit or the net/gross
reading you confirmed, entity types where ids from different systems meet, and a
functional-dependency fact or two where a rollup needs it. Every binding grounded in a
real column name, every vouched fact in a real invariant.
