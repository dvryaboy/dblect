# Demo scenarios

A library of small, realistic bugs a developer introduces into a currency-aware
jaffle shop, each paired with the minimal dblect declarations that catch it (or,
for the sound case, correctly stay quiet). Each scenario is self-contained, so they
never interfere with one another, and the test suite runs every one through
`dblect check` against a committed manifest.

## Layout

```
scenarios/
├── base/                     # one currency-aware jaffle: seeds + staging models
│                             #   raw_payments has a `currency` column; stg_payments carries it
├── README.md
└── cases/
    └── <case>/
        ├── story.md          # the developer's change, why it is a bug, what dblect says, the fix
        ├── overlay/          # files this case adds onto base (the new mart, usually)
        ├── dblect/           # the declarations (a real dblect/ package)
        ├── manifest.json     # committed; compiled from base + overlay by the refresh script
        └── expected.yml      # the findings dblect must produce
```

The base is the shared project. A case is a thin overlay on it plus its own
declarations, so adding a case does not touch the base or any other case.

## The cases

| Case | The change | dblect |
|---|---|---|
| `currency_creep` | source goes multi-currency; a stale `stg_payments` contract still says USD | flags the contradiction and its blast radius onto an undeclared rollup |
| `total_daily_revenue` | a new "revenue per day" report sums across currencies | flags the sum as not well typed |
| `order_rollup_sound` | revenue per order, with `order_id -> currency` declared | stays quiet (the dependency discharges the sum) |
| `revenue_by_customer` | a lifetime-revenue rollup, with the same `order_id -> currency` declared | still flags: a customer spans orders in different currencies, so the per-order dependency does not reach the wider grain |

`order_rollup_sound` and `revenue_by_customer` are a matched pair: both declare the
same `order_id -> currency` dependency, and it discharges the per-order sum while
leaving the per-customer sum flagged. The discharge is grain-precise, not a blanket
silence. `order_rollup_sound`'s mart is deliberately the same `sum(amount)` by
`order_id` as `currency_creep`'s `order_revenue`; the verdict differs only because
of what each case declares.

## Running

The tests read the committed `manifest.json` files, so they need no dbt:

```
uv run pytest tests/scenarios
```

## Regenerating the manifests

After changing the base or a case's overlay, regenerate its manifest. The script
compiles through a `jaffle_shop_duckdb` checkout (which supplies dbt-duckdb via its
own environment), so dblect's environment stays clean:

```
DBLECT_JAFFLE_DIR=/path/to/jaffle_shop_duckdb scripts/refresh_scenarios.sh [case ...]
```

## What the base does and does not document

The base documents only its **seeds** (the DAG leaves, which have no SQL to read
their columns from). The staging models carry just the columns they test, exactly
as stock jaffle does. dblect derives every other model's columns from its own SQL
as it walks the DAG, so the marts resolve `select *` and qualified references with
no per-column `schema.yml`. Reading dbt's `catalog.json` would remove the seed
documentation too (tracked in issue #77); a model dblect cannot analyze is reported
as "could not analyze" rather than passing silently.

## A note on shape

The aggregation cases declare `Money` on the staging model the marts read
directly. Currency that originates further upstream and flows through several hops
does not discharge yet, because companion bindings are not rebound through
projections (tracked in issue #76). Declaring the type at the layer that is
aggregated is a realistic pattern and exercises the discharge paths today; the
scenarios will move to the more natural seed-origin shape once that gap closes.

The open multi-currency declaration is named `value`, not `amount`: with the
currency carried in its own column, `Money` is a two-column tuple, and `value`
names the tuple while `amount` and `currency` stay the columns it binds to. The
pinned single-currency form keeps the column's own name (`amount:
Money.refine(currency=Currency.USD)`), where the currency is a constant rather than
a second column and the declaration types the one `amount` column in place.
