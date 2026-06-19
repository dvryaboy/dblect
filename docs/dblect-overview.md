# dblect

A semantic correctness framework for dbt analytics pipelines. It adds a typed declaration layer on top of your existing dbt project to catch a class of bugs where tests pass, the build is green, and the meaning of a column has quietly shifted: revenue switched from net to gross, an attribution window changed, a discount field started including coupons. dbt tests, Great Expectations, and Monte Carlo cover value-level checks well; dblect covers meaning-level checks.

## Using it

Run `dblect init` inside your dbt project. It scaffolds the `dblect/` directory, bootstraps dblect as a project dev dependency, parses your dbt project, generates editor stubs, and runs the zero-declaration audit. First real findings land in under a minute on typical projects, with zero declarations required. The audit catches ordering hazards, replay-determinism issues, and foreign-key fanout risks. `dblect check` re-runs it on demand thereafter, and reports meaning-level findings once you declare types.

From there:

1. Declare semantic types for the columns that matter. A `Revenue` type carries parameters like `contains_tax` and `currency`; refinements like `RevenueNet` pin those values. Annotate the dbt models that produce or consume them.
2. Declare contracts on model classes: conservation across boundaries, cardinality, idempotence, replay-determinism class, late-data tolerance.
3. Run `dblect check` in CI. The framework propagates types along the dbt DAG and runs property-based tests against your models, with failing tests shrunk to minimal counterexamples.

Findings surface in CI in the same format your linters and type checkers use. The framework remembers failing examples across runs and biases generation toward known edge cases.

## Approach

Two complementary halves sharing one substrate.

**Static.** Type propagation across the dbt DAG using sqlglot column-level lineage. Catches semantic shifts where a column gets retyped upstream and a downstream contract no longer holds, without running anything. This is what catches the revenue-from-net-to-gross class of bug at PR review time.

**Runtime.** Property-based testing via Hegel (a Hypothesis-descended library), with data generated from declared types and foreign-key relationships honored across multi-table fixtures. Catches local logic errors that pass static checks (join fanouts, NULL handling, window edge cases) by running models on adversarial inputs in dbt-duckdb.

Declarations are Python: Pydantic-style classes for types and contracts, with operator-overloaded column proxies for expressions. dbt itself is unchanged. dblect reads `manifest.json` and adds declarations alongside dbt's existing structure, with `schema.yml` consulted for free signal (existing `relationships` tests act as foreign keys, existing `meta` blocks carry optional declarations).

## Optional: AI-assisted authoring

dblect exposes an MCP server that lets Claude or other AI coding assistants read your manifest, propose semantic types, draft contracts from existing model SQL, and analyze flag-flip impact. The framework works fully without LLM access; the MCP surface adds an authoring accelerator for projects with many models to onboard.
