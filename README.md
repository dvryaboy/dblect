# dblect

A semantic correctness framework for dbt analytics pipelines. dblect catches the bugs that survive a green build: SQL is valid, the tests pass, the row counts look right, but the *meaning* of a column has quietly shifted. Revenue went from net to gross, and now your discount calculations are getting applied to taxes when they shouldn't. A staging model started carrying euros alongside dollars and a downstream `sum()` added them together anyway.

Existing tools are great for checking basic validity: is the syntax correct, is this column null, is it unique, is it within range. dblect checks meaning: is this still the same quantity it was last quarter, and does this aggregation combine things that are actually comparable. It reads your existing dbt project and adds a typed declaration layer alongside it. dbt itself is unchanged.

## How it works

dblect reads the SQL your models compile to (after dbt has rendered Jinja, so it sees macros, conditionals, and `ref`s the way the warehouse will) and works in two layers.

**An audit that needs no declarations.** It walks every model's SQL and flags structural hazards: the SQL footguns that are easy to write, hard to spot in review, and invisible to a row-count check. This runs the moment you point it at a project.

**A typed declaration layer.** You annotate the columns that matter with semantic types and contracts written in Pydantic-flavored Python.  Just annotate what you need; dblect propagates those types along the dbt DAG using column-level lineage, so a claim you make on one staging model is carried through to all models downstream. dblect understands the effects of operations like joining, grouping, and distincting - no need to re-annotate.

Findings come out in the same shape as your linter or type checker, with `text` for terminals, `json` for CI and editors, and `sarif` for GitHub code scanning and other SARIF-aware surfaces.

## What it catches

### Structural hazards, with zero declarations

Here is a snippet from the stock jaffle shop `customers.sql`, the model in everyone's first dbt project. It computes each customer's lifetime payment total:

```sql
customer_payments as (
    select
        orders.customer_id,
        sum(amount) as total_amount
    from payments
    left join orders on payments.order_id = orders.order_id
    group by orders.customer_id
)
```

The `left join` keeps every payment, even ones whose `order_id` finds no matching order. For those, `orders.customer_id` is `NULL`, so the `group by` rakes all of them into one `NULL` bucket and sums unrelated payments together. The total looks like a real customer's lifetime value. It belongs to nobody. No test fails.

The same pass also flags a `WHERE` on the nullable side of an outer join (silently inverts it to an inner join, dropping the very rows the outer join meant to keep), `COALESCE` on a join key (masks "no match" as a real value), window and `array_agg`/`string_agg` calls with no `ORDER BY` (nondeterministic output), nondeterministic builtins like `current_timestamp()` in join keys or partition clauses, joins that fan out because the joined-in side is not unique on the join key, and reads of a dbt snapshot with no temporal filter (you get every historical version of every row, not the current state). These need nothing declared: dblect uses whatever keys your `schema.yml` and native constraints already assert and stays quiet where it has no grounds to speak.

```
$ dblect check .
dblect: 1 finding over 5 models (0 contracts resolved, 5 scanned, 0 predicate(s) collected)

coverage:
  resolution: 100.0% of columns (27/27)
  grounding: domain_type 0/27; functional_dependency 0/5
  contract columns checkable: 0/0
  worlds: 1 (base); no flag axes enumerated

structural findings:
  models/customers.sql  (model.jaffle_shop.customers)
    L44  null_group_after_outer_join
        GROUP BY orders.customer_id references column(s) from nullable join side (orders); unmatched rows collapse into a NULL group
        snippet: orders.customer_id
```

### You can suppress these warnings

Sometimes the catch-all bucket is on purpose: orphaned payments get pooled deliberately and handled downstream. When a finding is a known, intended choice, you tell dblect so with a SQLFluff-style `-- noqa` comment, the same syntax dbt Fusion's `dbt lint` honors, and it moves from a finding to a recorded suppression rather than noise you learn to scroll past:

```sql
    left join orders on payments.order_id = orders.order_id
    group by orders.customer_id  -- noqa: DBLECT_NULL_GROUP_AFTER_OUTER_JOIN
```

```
$ dblect check .
dblect: 0 findings over 5 models (0 contracts resolved, 5 scanned, 0 predicate(s) collected)

suppressed:
  models/customers.sql:L44  null_group_after_outer_join  suppressed by noqa: DBLECT_NULL_GROUP_AFTER_OUTER_JOIN @ L44
```

A bare `-- noqa` silences every dblect finding on its line; `-- noqa: DBLECT_<KIND>` silences one detector (the code is `DBLECT_` plus the finding kind uppercased). Codes that do not start with `DBLECT_` are real lint rule codes that belong to `dbt lint`, so `-- noqa: RF01, DBLECT_JOIN_FANOUT` speaks to both tools at once. Every suppression is logged in the `suppressed:` section, so a silenced finding stays visible in review.

### Catch meaning shifts, by adding a small amount of higher types

A dbt project encodes meaning in SQL and in data engineers' heads and almost nowhere a tool can read: `order_total` is net of discounts but gross of tax, `amount` is dollars until the day someone adds a `currency` column and a EUR row. dblect's declaration layer is where you write that meaning down, once, in Python that sits beside your project and never touches your models.

Two kinds of declaration carry it, and if you know Pydantic they will look familiar:

- A **`DomainType`** is a type that carries meaning. You build it from fields, so a `Money` is an `amount` together with a `currency`. You put it on a column the way you would `Decimal`, except it also knows what the number means. **Refining** a type pins a field to a value: `Money.refine(currency=Currency.USD)` is `Money` narrowed to dollars.
- A **`ModelContract`** binds those types to one dbt model's columns. Each field names a column and gives it a type. You declare only the columns that matter; the rest still flow through the structural audit, they just carry no domain type. 

You type a column the day its meaning starts to matter, and dblect propagates the type along the DAG, so a claim you make on one model is checked against every model that reads from it. (`Money` and `Currency` here come from `dblect.demo`, a small starter vocabulary you copy and extend; real projects declare their own units and categories.)

The payments business is multi-currency: payments carry their own currency, and the team writes that down once, on the staging model. That is the whole declaration:

```python
class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    value: Money.columns(amount="amount", currency="currency")
```

(`value` is a virtual column: one `Money` concept sitting on top of two real columns, `amount` and `currency`.)

Someone adds a "revenue per day" report that sums payments for each date:

```sql
select o.order_date, sum(p.amount) as revenue
from {{ ref('stg_payments') }} as p
join {{ ref('stg_orders') }} as o on p.order_id = o.order_id
group by o.order_date
```

A single day holds payments in several currencies, so `sum(amount)` adds dollars to euros to pounds. The number is plausible, the build is green, and the report is quietly wrong:

```
$ dblect check .
dblect: 1 finding over 4 models (1 contracts resolved, 4 scanned, 0 predicate(s) collected)

declaration findings:
  aggregation_not_well_typed  model.jaffle_shop.total_daily_revenue.revenue
      reducing 'revenue' with sum(amount): its per-row companion 'currency' is not held constant by grouping on 'order_date'; the aggregation is not well typed
      models/marts/total_daily_revenue.sql
```

One declaration on the staging model was enough. dblect carried the currency down the DAG and flagged the sum on a mart nobody typed, before any data ran. The fix is to convert to a common currency before summing, or to group by currency so each row is honestly per-currency. The same finding lands on a lifetime-revenue-per-customer rollup, for the same reason: a customer transacts across orders in different currencies.

The check is precise, not a blanket suspicion of every `sum`. Sometimes a sum across rows is exactly right. An order can be settled by several payments, split across two cards say, and those are always in one currency, so revenue per order is sound. You tell dblect that fact with an `@contract` method:

```python
class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    value: Money.columns(amount="amount", currency="currency")

    @contract
    def one_currency_per_order(self):
        return self.order_id.determines(self.currency)
```

Grouping by `order_id` now holds the currency constant within each group, so `sum(amount)` per order checks clean and dblect stays quiet. The same dependency does **not** silence the sum grouped by *customer*: a customer spans many orders that can differ in currency, so the lifetime-revenue rollup still lights up. The discharge is grain-precise, not a blanket exemption.

The runnable versions of these live in [`tests/fixtures/scenarios`](tests/fixtures/scenarios), each with a short `story.md`. The test suite runs every one through `dblect check`.

## Install

```bash
uv add --dev dblect          # or: pip install dblect
```

dblect needs the compiled SQL dbt produces. Either run `dbt compile` yourself and let dblect read `target/manifest.json`, or let dblect invoke `dbt compile` for you, which needs dbt installed and a working profile:

```bash
uv add --dev "dblect[dbt-core]"
```

dblect runs on Python 3.11 and newer. It reads manifests written by dbt 1.8 through 1.11.7; the `[dbt-core]` extra pins that range. A manifest from a newer dbt is not yet modeled by the parser dblect uses (tracked in #106), so point dblect at a manifest within the range or pass one explicitly with `--manifest`.

Finding line numbers refer to the compiled SQL the analyzer parsed, not to the on-disk `.sql` template. Every finding also carries the model's source file path, so you can open the source and locate the construct from there.

## Quick start

Inside any dbt project:

```bash
dblect check .       # structural hazards, zero declarations needed
dblect init .        # scaffold dblect/ and generate editor stubs from your manifest
dblect check .       # now also reports meaning-level findings from the types you declared
```

`dblect check` produces structural findings in under a minute on typical projects, with no declarations required. From there, declare semantic types on the columns that matter and run `dblect check` in CI.

Authoring those declarations is where an AI coding agent helps. Install the bootstrap skill into your agent and let it walk the project, propose types on the columns whose meaning matters, and run the check loop with you:

```bash
dblect setup claude .   # or: cursor, codex
```

This writes a harness-specific skill (`.claude/skills/`, `.cursor/rules/`, or an `AGENTS.md` block); run `dblect setup <target> --print` to review it first.

See the [demo walkthrough](docs/design/demo_walkthrough.md) for an end-to-end tour against `jaffle_shop_duckdb`, and [docs/current_state/architecture.md](docs/current_state/architecture.md) for what is built today.

## Severity and CI

Every finding carries a severity, and that severity is what turns `dblect check` into a CI gate:

- **error** is a correctness hazard: the query can return wrong rows by silently dropping, duplicating, or mis-grouping them. A fan-out that double counts, a `WHERE` that inverts an outer join into an inner one, a nullable join key that drops unmatched rows. The output is wrong and no test fails.
- **warn** is a determinism smell: each run is correct on its own, but the result is not pinned and can drift between runs. An `array_agg` or window with no `ORDER BY`, a top-level `LIMIT` with no total order, a top-n `LIMIT` inside an aggregate whose order key has ties, a nondeterministic builtin like `current_timestamp()` in a key.
- **info** is an observation worth surfacing but not worth acting on by default.

The report always prints every finding. `--fail-on` sets only the exit code: the run exits non-zero when an unsuppressed finding sits at or above the threshold, and `0` below it. The default threshold is `warn`.

```bash
dblect check .                  # default: fail on warn and above
dblect check . --fail-on error  # gate only on correctness hazards; smells still print
dblect check . --fail-on info   # strictest: any finding fails the run
dblect check . --no-fail        # always exit 0, report only
```

The error/warn line is wrong-rows versus drift. An `error` means the data is incorrect today, so fix it. A `warn` means the data is correct today but not reproducible, so a rerun, a backfill, or a late-arriving row can change it. A determinism smell is real even when it is not urgent: `array_agg(order_id order by amount desc limit 10)` returns a genuine top ten by amount every run, yet a metric over those ten orders (their average basket size, say) can shift between runs when amounts tie at the cutoff, so add a stable tiebreaker where reproducibility matters.

Two levers cover the smells you have already weighed. When one is intentional, record it with `-- noqa: DBLECT_<KIND>` so it moves to the `suppressed:` section instead of failing the build. When you want CI to block only on correctness and treat reproducibility as advisory, run `--fail-on error`. Start at the default so the smells stay visible, and tighten or loosen from there.

## Status

Pre-alpha, and useful now. The structural audit and the typed-declaration check both run end-to-end against real dbt projects. The runtime layer (property-based testing of models against generated adversarial data, replay-determinism via differential execution) is designed and on the way; see [docs/](docs/) for the design notes and [questions_and_decisions.md](questions_and_decisions.md) for the decisions log.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                  # install dev environment
uv run pytest            # run tests
uv run ruff check        # lint
uv run ruff format       # format
uv run pyright           # type-check (strict)
```

## License

Apache 2.0. See [LICENSE](LICENSE).
