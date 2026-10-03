# Running dblect on SQL that is not a dbt project

dblect reads a dbt `manifest.json`: compiled SQL per model, the columns of each source, and the tests that declare keys. Any SQL can be given that shape. A folder of queries, the SQL inside an Airflow DAG, or a benchmark's answers all work once each query becomes a model over declared sources.

There are two ways to get there. Wrapping the SQL in a throwaway dbt project is the simpler one and needs only `dbt-duckdb`. Writing the manifest directly skips dbt, which pays off for thousands of queries.

## Wrap the SQL in a throwaway dbt project

A project needs four things: `dbt_project.yml`, a profile, a sources file, and one model per query.

```yaml
# dbt_project.yml
name: wrapped
version: "1.0"
profile: wrapped
```

```yaml
# profiles.yml. Compiling needs no data, so an in-memory database is enough.
wrapped:
  target: dev
  outputs:
    dev: {type: duckdb, path: ":memory:"}
```

The sources file describes every table the queries read. Give each column its `data_type`. That way dblect knows the columns without a `catalog.json`, which would need a real warehouse. Declare `unique`, `not_null` and `relationships` tests for the keys the database has. These tests are what dblect reasons from, so they carry most of the value. Without them it knows no key and has little to say about joins.

```yaml
# models/sources.yml
version: 2
sources:
  - name: raw
    schema: main
    tables:
      - name: customers
        columns:
          - {name: customer_id, data_type: integer, data_tests: [unique, not_null]}
          - {name: credit_limit_cents, data_type: bigint}
      - name: orders
        columns:
          - {name: order_id, data_type: integer, data_tests: [unique, not_null]}
          - {name: customer_id, data_type: integer}
```

Each query becomes `models/<name>.sql`, with its table names rewritten to `source()` calls:

```sql
-- models/active_credit.sql
select sum(c.credit_limit_cents) as credit_cents
from {{ source('raw', 'customers') }} c
join {{ source('raw', 'orders') }} o on o.customer_id = c.customer_id
```

Then compile and check:

```bash
dbt compile --project-dir wrapped --profiles-dir wrapped
dblect check wrapped --manifest wrapped/target/manifest.json
```

```
structural findings:
  models/active_credit.sql  (model.wrapped.active_credit)
    L3 (compiled)  warn  join_fanout
        JOIN to main.orders on (customer_id) isn't covered by any known uniqueness key on main.orders (known: (order_id)); ...
```

Domain-type declarations go in `wrapped/dblect/`, as in any project.

When the queries are written for another engine, either transpile them to DuckDB with `sqlglot.transpile(sql, read=..., write="duckdb")` before wrapping, or keep them as written and pass `--dialect` to `dblect check`. Transpiling is the tested path.

## Write the manifest directly

`Manifest.from_raw` validates its input against dbt's published manifest schema through `dbt-artifacts-parser`, so a manifest built from scratch has to match that schema field for field. The practical route is to compile one small wrapper project as above, then clone its nodes. Take one source node, one model node, and one test node for each kind of test you declare. For every table and query, copy the matching template and set:

- on a source: `unique_id`, `name`, `identifier`, `source_name`, `database`, `schema`, `relation_name`, `fqn`, and `columns` with each column's `data_type`;
- on a model: `unique_id`, `name`, `alias`, `fqn`, `relation_name`, `compiled_code` (with `compiled: true`), `sources`, `depends_on.nodes`, and `original_file_path`;
- on a test: `unique_id`, `name`, `column_name`, `test_metadata.kwargs` (`column_name`, `combination_of_columns` for `dbt_utils.unique_combination_of_columns`, or `to` and `field` for `relationships`), and `depends_on.nodes` naming the source it tests.

Empty `parent_map`, `child_map` and `group_map` are fine, since dblect builds its graph from `depends_on`. Then run `dblect check <dir> --manifest <file>`, where `<dir>` holds any declarations under `dblect/` and needs no `dbt_project.yml`.

## Gotchas

- **dblect reads only `compiled_code`.** A model whose compiled code is absent or stale shows up as a coverage miss, and nothing reads `run_results.json`. Recompile after every edit.
- **Declare only keys that hold on the data.** A `unique` test the data would fail lets dblect accept a join that really does fan out. Many engines leave declared constraints unenforced: SQLite enforces neither foreign keys nor `NOT NULL` on a non-integer primary key by default. Run each test's violation query against the data before declaring it.
- **A foreign key that names only the parent table** points at the parent's primary key. When the parent lacks the named column, skip the test. SQLite quietly binds an unknown column inside a subquery to the outer query, so its violation query checks the wrong thing.
- **Keep identifier case consistent.** Write table and column names in one case, in the SQL and the sources alike. A quoted mixed-case name in one place and a lowercase key in the other can fail to match.
- **Read the coverage sections, not just the findings.** A model dblect could not analyze is listed under `unbuilt` or `skipped` with a reason (keyed by `unique_id` in `-f json` output), and it contributes no findings. A clean report with many skipped models has checked little.
- **Pass absolute paths** when calling `dblect` from a script whose working directory is somewhere else.
