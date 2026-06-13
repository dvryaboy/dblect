# dbt compile-time configuration: a taxonomy for static analysis

Status: research note (input to the config / flag-world design)
Audience: anyone working on var discovery, the config discoverer, or the flag-world bridge. This is the field-survey half of that design; the design itself lives in [`config-and-flag-worlds.md`](../config-and-flag-worlds.md).

This note catalogues the mechanisms by which a dbt project's model SQL changes meaning or shape at compile time. The organizing distinction for a static analyzer is **value substitution** (compiled SQL keeps the same shape; only a literal changes) versus **control flow** (compiled SQL is structurally different across inputs). dbt is a Jinja-over-SQL templating system: every model `.sql` file is rendered to a SQL string at `dbt compile` / `dbt run` time, and every mechanism below feeds that render.

Sources are dbt's own documentation, linked inline. The frequency estimates in section 6 are explicitly flagged as judgment, not measurement.

---

## 1. `var()` usage patterns

`var('name', default)` reads a project variable. Values come from `dbt_project.yml` (the `vars:` block) and can be overridden at the command line with `--vars '{...}'` (CLI wins). The function is available in `.sql` and `.yml` files but not in `profiles.yml` or `packages.yml`.

### (a) Value substitution: same shape, different literal

The var renders directly into the SQL text as a literal. The parse tree of the compiled SQL is invariant under the var's value.

```sql
-- a literal in a WHERE predicate
select * from events
where event_type = '{{ var("event_type", "activation") }}'
```

```sql
-- a numeric literal
select * from {{ ref('orders') }}
limit {{ var('row_limit', 1000) }}
```

```sql
-- a date boundary
where order_date >= '{{ var("start_date", "2020-01-01") }}'
```

For the analyzer: these are leaf-literal swaps. Column set, join structure, row-shape, and grain are identical across var values. The semantic risk is value-dependent (a value that changes the result set, or the meaning of a column, but not the schema).

### (b) Control flow: structurally different SQL

The var gates Jinja branching or iteration, so different values emit different SQL.

```sql
-- boolean toggle gating a projected column
select order_id, amount {% if var('include_tax', false) %}, tax_amount{% endif %}
from {{ ref('orders') }}
```

```sql
-- list-valued var driving a pivot (the classic "payment methods" pattern)
select
    order_id,
    {% for payment_method in var('payment_methods') %}
    sum(case when payment_method = '{{ payment_method }}'
             then amount end) as {{ payment_method }}_amount
    {%- if not loop.last %},{% endif %}
    {% endfor %}
from {{ ref('payments') }}
group by 1
```

Here the **column set itself** is a function of the var. Add a payment method and the output schema changes. A boolean `enable_x` var that wraps a `join` or a `union` changes the effective shape and potentially the grain.

This is where "flip a flag and the meaning changes" lives. The compiled output's column list, join graph, and grain can all be functions of the var.

A var read inside `config()` (for example `materialized="{{ var('mat', 'view') }}"`) makes a config key, and therefore the materialization semantics in section 5, itself control-flow-dependent.

Sources: [var function](https://docs.getdbt.com/reference/dbt-jinja-functions/var), [Project variables](https://docs.getdbt.com/docs/build/project-variables)

---

## 2. `env_var()` usage patterns

`env_var('NAME', default)` reads an OS environment variable. It is available almost everywhere dbt renders Jinja: `profiles.yml`, `dbt_project.yml`, `sources.yml`, `schema.yml`, and model `.sql`. Env vars are always strings, so projects pipe them through `| int`, `| as_bool`, and similar filters.

| Use case | Example | Class |
|---|---|---|
| Credentials / connection in `profiles.yml` | `password: "{{ env_var('DBT_PASSWORD') }}"` | Value substitution (not in SQL; connection only) |
| Schema / database / target switching | `dataset: "{{ env_var('DBT_DATASET') }}"` | Value substitution (changes *where* objects land, not SQL shape) |
| Secrets (`DBT_ENV_SECRET_` prefix) | only in `profiles.yml` / `packages.yml`; scrubbed from logs | Value substitution (disallowed in model SQL by design) |
| Config toggles in `dbt_project.yml` | `+materialized: "{{ env_var('DBT_MATERIALIZATION', 'view') }}"` | **Control flow** (drives a config key that changes materialization semantics) |
| Custom metadata (`DBT_ENV_CUSTOM_ENV_`) | surfaced via `dbt_metadata_envs` | Value substitution (comments / artifacts) |

Most `env_var()` usage is value substitution targeting *connection and placement* (which schema / database / account), not SQL body shape. The control-flow case appears when an env var feeds a `config()` key (materialization, enabled) or, less commonly, gates an `{% if %}` in model SQL. Env-var changes trigger only partial re-parse in dbt, whereas `vars` changes force a full re-parse.

Source: [env_var function](https://docs.getdbt.com/reference/dbt-jinja-functions/env_var)

---

## 3. `target` context (`target.name`, `target.schema`, `target.database`)

`target` exposes the active connection profile. Adapter-independent attributes: `target.name`, `target.schema`, `target.type`, `target.threads`, `target.profile_name`. Adapter-specific: `target.database` / `target.project` / `target.dataset`, `target.warehouse`, `target.role`.

The dominant SQL-body pattern is a dev / prod / ci dispatch on `target.name`, which is **structurally identical to a control-flow var** (it gates an `{% if %}` block):

```sql
-- limit data volume in dev only
select *
from {{ source('web_events', 'page_views') }}
{% if target.name == 'dev' %}
  where created_at >= dateadd('day', -3, current_date)
{% endif %}
```

Two sub-cases for the analyzer:

- `target.name` / `target.type` inside `{% if %}`: **control flow** (same family as a boolean var). The set of reachable target names is closed and known from `profiles.yml`, so the analyzer can in principle enumerate the branches.
- `target.schema` / `target.database` substituted into a `ref` / `source` / fully-qualified name: **value substitution** that changes object placement, not SQL shape.

A subtlety: `target` values are not knowable from the project repo alone; they live in `profiles.yml`, often outside the repo or in CI secrets. The *branch structure* is in the SQL, but which branch fires depends on environment.

Source: [target | dbt Developer Hub](https://docs.getdbt.com/reference/dbt-jinja-functions/target)

---

## 4. `is_incremental()` and incremental models

`is_incremental()` is a compile-time boolean that produces **structurally different SQL for the same model** depending on warehouse state. Per the docs, it returns `True` only if all of:

1. the model already exists as a table in the database,
2. the `--full-refresh` flag is **not** passed, and
3. the model is configured `materialized='incremental'`.

So on the **first run** (target table absent) it is `False` and the model compiles like a full table build over all source rows. On a **subsequent run** it is `True` and the `{% if is_incremental() %}` filter is included, processing only new or changed rows. A `--full-refresh` rebuild forces it back to `False`. The docs state explicitly: *"the SQL in your model needs to be valid whether `is_incremental()` evaluates to `True` or `False`."*

Canonical model:

```sql
{{
  config(
    materialized='incremental',
    unique_key='event_id',
    incremental_strategy='merge'
  )
}}

select
    event_id,
    event_time,
    user_id,
    payload
from {{ ref('app_events') }}

{% if is_incremental() %}
  -- only on incremental runs; {{ this }} is the existing target table
  where event_time >= (select coalesce(max(event_time), '1900-01-01') from {{ this }})
{% endif %}
```

For the analyzer: this is a two-state compile of one source file. The SQL bodies differ (the `WHERE` is present or absent), and the *effect* differs more than the body suggests, since the same compiled SELECT is wrapped by dbt-generated DML (CREATE-AS on first run, MERGE / DELETE+INSERT / INSERT on incremental runs, per section 5). The boolean is not an author input; it is derived from warehouse state plus the `--full-refresh` flag, so unlike a var it cannot be set in the repo. Both states must be considered reachable.

Source: [Configure incremental models](https://docs.getdbt.com/docs/build/incremental-models)

---

## 5. Model `config` keys and their semantic implications

### `materialized`

- `view`: compiled SELECT becomes `CREATE VIEW`. No stored rows; re-evaluated on read.
- `table`: `CREATE TABLE AS SELECT`, fully rebuilt each run.
- `incremental`: table on first build, then incremental DML (section 4 plus strategies below).
- `ephemeral`: not built as a DB object; the model's SQL is **inlined as a CTE** into downstream models. An ephemeral model has no independent relation; its lineage and predicates fold into consumers.
- `materialized_view`: a warehouse-managed materialized view; refresh semantics delegated to the warehouse.

### `incremental_strategy` x `unique_key` (the dedup-critical part)

This is the part most often gotten wrong, so stated precisely from the docs:

| Strategy | Uses `unique_key` | Dedups / updates existing rows? | DML mechanism |
|---|---|---|---|
| `append` | No | **No** (pure insert, will create duplicates) | `INSERT` |
| `merge` | Optional | **Yes if `unique_key` set**; **behaves like `append` if not** | `MERGE` (update on key match, insert otherwise) |
| `delete+insert` | Required | **Yes** | `DELETE` matching keys, then `INSERT` |
| `insert_overwrite` | No (partition-based) | Replaces whole partitions, no row-level dedup | partition replace |
| `microbatch` | No (time-batch based) | per-batch replacement | batched |

Load-bearing facts (from dbt docs):

- **`append` does not deduplicate.** If the same record appears multiple times in the source, it is inserted again, potentially resulting in duplicate rows.
- **`merge` without `unique_key` degrades to append.** With a `unique_key`, if the key already exists in the destination table `merge` updates the record, so duplicates do not accumulate.
- **`delete+insert`** deletes rows for the `unique_key` then inserts; semantically equivalent to `merge` for dedup but a different mechanism (and `unique_key` is required).
- **`insert_overwrite` ignores `unique_key`** entirely; it operates on partitions, not rows.

The critical, non-obvious takeaway: **setting `unique_key` does NOT by itself guarantee uniqueness.** Enforcement is a function of the (`incremental_strategy`, `unique_key`) *pair*. `unique_key` + `append` gives no enforcement (silent duplicates). `unique_key` + `merge` / `delete+insert` enforces. `merge` + no `unique_key` gives no enforcement. A `unique` data test on the key catches violations only at test time, not at write time.

Adapter defaults differ: Snowflake / BigQuery / Redshift / Postgres default to `merge`; Spark defaults to `append`. The *same model* with no explicit strategy can dedup on Snowflake and silently duplicate on Spark.

### Other config keys

- **`on_schema_change`** (`ignore` default, `append_new_columns`, `sync_all_columns`, `fail`): governs what happens to the target when the model's column set changes between runs. Relevant because sections 1(b) and 4 can make the column set itself variable.
- **`full_refresh`**: a config-level override; `full_refresh: true` forces a rebuild every run (and forces `is_incremental()` False); `false` prevents even `--full-refresh` from rebuilding.
- **`cluster_by` (Snowflake) / `partition_by` (BigQuery)**: physical layout; no effect on the compiled SELECT's logical shape but affects `insert_overwrite` partition targeting.
- **`enabled`**: `enabled: false` removes the model from the graph entirely (not compiled, not built, not a valid `ref` target). Often itself set via `var()` / `env_var()`, making node existence a flag-dependent fact.

Sources: [About incremental strategy](https://docs.getdbt.com/docs/build/incremental-strategy), [Configure incremental models](https://docs.getdbt.com/docs/build/incremental-models)

---

## 6. How often are vars value-substitution vs control-flow?

There is no authoritative published measurement, so the following is informed estimation from the conventions in jaffle_shop, dbt-utils, and common analytics-engineering practice. This is judgment, not a cited statistic.

The majority of `var()` usage is **value substitution**: date bounds, row limits, a configurable lookback window, a threshold, or a database / schema name pulled from a project var. The estimate here is that **control-flow vars (those that gate an `{% if %}` / `{% for %}` and change compiled SQL shape) are a minority, on the order of a quarter to a third of var usages**, clustered in a few recognizable idioms:

- list-valued vars driving `{% for %}` pivots (the payment-methods pattern),
- boolean feature / section toggles (`enable_x`, `include_pii`, `backfill_mode`),
- environment gating sometimes routed through a var instead of `target`.

Supporting signal: the dbt-core "future of vars" discussion frames vars overwhelmingly around environment-based configuration and DRY-ing reusable values (substitution-flavored), and does not center feature-flags / control-flow as the primary use case. The most common *control-flow* gate in practice is arguably not a var at all but `is_incremental()` and `target.name`, which are structural by nature.

Source: [The future of vars, dbt-core Discussion #6170](https://github.com/dbt-labs/dbt-core/discussions/6170)

---

## 7. Prior art: "what breaks when I flip a flag" / testing across configurations

There is no single named dbt feature for this, but several established practices and primitives are relevant:

- **`--vars` override at the CLI**, designed for temporarily overriding configuration without changing committed project files. This is the hook a CI matrix uses to run the project under multiple var sets. ([var function](https://docs.getdbt.com/reference/dbt-jinja-functions/var))
- **GitHub Actions matrix builds** are the common mechanism teams use to run dbt across several `--vars` / `--target` combinations in parallel; general CI infrastructure, not dbt-specific.
- **dbt unit tests (dbt >= 1.8)** explicitly support overriding the output of `vars`, `env_var`, and macros, and the docs call out using this to unit-test incremental models in both full-refresh and incremental modes. This is the closest first-class dbt feature to "test both states of a compile-time flag." ([Unit tests](https://docs.getdbt.com/docs/build/unit-tests))
- **Slim CI / state comparison** (`dbt build --select state:modified+`) tests only changed nodes against a deferred prod manifest. The state-comparison caveats doc warns that environment-dependent logic (for example `{% if target.name == ... %}`, `is_incremental()`) makes state comparison and "does this change anything" reasoning subtle, a direct acknowledgment in dbt docs that flag-dependent compilation complicates change detection. ([Caveats to state comparison](https://docs.getdbt.com/reference/node-selection/state-comparison-caveats), [Set up CI](https://docs.getdbt.com/guides/set-up-ci))

The honest framing: the community has the *primitives* (CLI var overrides, target dispatch, unit-test overrides, state comparison) and acknowledges the *problem* (state-comparison caveats around environment-dependent SQL), but there is no established tool that systematically answers "enumerate the structurally distinct compilations of this model across the reachable flag space and tell me what differs." That gap is the space dblect's flag-world analysis is entering.

---

## Summary classification

| Mechanism | Value substitution | Control flow (shape-changing) |
|---|---|---|
| `var()` literal in SQL | yes | |
| `var()` in `{% if %}` / `{% for %}` | | yes |
| `env_var()` for creds / schema / db | yes | |
| `env_var()` feeding a `config` key | | yes (materialization / enabled) |
| `target.schema` / `database` in names | yes | |
| `target.name` in `{% if %}` | | yes |
| `is_incremental()` | | yes (two states, state-derived) |
| `config.materialized` / `incremental_strategy` / `unique_key` | | yes (DML + dedup semantics) |
| `enabled` | | yes (node existence) |

The highest-value, least-obvious correctness target is section 5's **(`incremental_strategy`, `unique_key`) pair**: uniqueness is enforced only under `merge`-with-key or `delete+insert`, silently absent under `append` or `merge`-without-key, and the default strategy is adapter-dependent.

## Sources

- [var function](https://docs.getdbt.com/reference/dbt-jinja-functions/var) and [Project variables](https://docs.getdbt.com/docs/build/project-variables)
- [env_var function](https://docs.getdbt.com/reference/dbt-jinja-functions/env_var)
- [target context](https://docs.getdbt.com/reference/dbt-jinja-functions/target)
- [Configure incremental models](https://docs.getdbt.com/docs/build/incremental-models)
- [About incremental strategy](https://docs.getdbt.com/docs/build/incremental-strategy)
- [Unit tests](https://docs.getdbt.com/docs/build/unit-tests)
- [Caveats to state comparison](https://docs.getdbt.com/reference/node-selection/state-comparison-caveats)
- [Set up CI](https://docs.getdbt.com/guides/set-up-ci)
- [The future of vars, dbt-core Discussion #6170](https://github.com/dbt-labs/dbt-core/discussions/6170)
</content>
</invoke>
