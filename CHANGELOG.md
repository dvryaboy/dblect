# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-11

The first published release: the base-world static analyzer, packaged for
`pip install dblect`. It reads a compiled dbt manifest, propagates column-level
facts through the DAG, and reports structural and declaration-level findings the
user can navigate to and act on. No execution and no LLM are required. The
default severities ship calibrated against a corpus of real projects across
DuckDB, Snowflake, and Spark. The runtime half (property-based execution,
replay-determinism) and the flag/var-world analysis are deferred to later
releases.

### Added

**Manifest and SQL ingestion**

- Typed dbt manifest ingestion: `Manifest`, `Node`, and a `Dag` with topology,
  parsed from `manifest.json`. Reads manifests written by dbt 1.8 through
  1.11.7.
- Analysis over each model's compiled SQL (not the raw Jinja), parsed once per
  run and shared across detectors via a sqlglot wrapper. Multi-statement models
  parse into their component statements, and a compiled artifact that has gone
  stale against its source is surfaced as a coverage miss rather than analyzed
  silently.
- `catalog.json` ingestion for seed and source columns, so undocumented DAG
  leaves resolve.
- Source-Jinja front end that discovers `var` and `env_var` references, and a
  typed view of the manifest's macro registry.

**Column-level lineage substrate**

- A K-relations / semiring propagator carrying where-provenance through the DAG,
  with CTE and UNION intermediates materialized in the lineage graph.
- The `lineage.facts` substrate: one property propagator over a shared fact
  representation, with nullability discoverers backed by the manifest.
- Conditional facts that carry their guarding predicate, a predicate-implication
  engine, and a predicate-flow property that accumulates each relation's row
  filter, so conditional uniqueness, candidate keys, and `NOT NULL` activate
  across relations and at intra-model scopes.
- Nested-field (STRUCT) lineage with explicit `UNNEST` grain.

**Uniqueness and determinism analysis**

- Uniqueness facts from dbt declarations and from structural proof, an
  ordering-key detector and a join-fan-out detector over substrate keys, and
  propagation of uniqueness through SQL operations and across model
  dependencies.
- Candidate keys derived through surrogate-hash columns; seeds and snapshots
  spanned as test targets.
- Determinism detectors for a materialized model whose row set depends on an
  arbitrary slice: a non-deterministic top-level `LIMIT`, and an `ORDER BY` that
  does not fully order the rows it feeds a top-n cut.

**Domain-type contracts (the declaration DSL)**

- The authoring core: `DomainType`, `ModelContract`, `Field`, and the bridge
  that lowers a contract to substrate facts.
- A contract and proxy layer: an expression AST, column proxies, the
  `@contract` decorator, and fact constructors.
- Companion-binding column properties (the currency story), may/must refinement
  for widened magnitudes, and domain-type transfer through joins
  (functional-dependency-through-join, outer-join taint, join-key and fan-out
  signals).
- A config discoverer that grounds an incremental model's `unique_key`, and
  fact-level flag-world plumbing.
- Aggregate behavior as a first-class combine/select/count classification
  (`dblect.sql.aggregates`), keyed on the sqlglot node type so it covers every
  dialect at once. It is the single source of truth for both arming the
  coherence guard and phrasing the `aggregation_not_well_typed` finding, which
  names the aggregate, the column it reduced, the per-row companion left
  varying, and the grouping that failed to hold it. `count`, `count_if`, and
  `approx_count_distinct` over a typed magnitude are row counts and stay quiet;
  `stddev`, `variance`, `kurtosis`, `skewness`, `median`, `mode`, and the
  quantile/percentile family flag a mixed-currency reduction; `min`/`max` widen
  their result tag on a varying companion under the lenient default (#115).
- Wider scalar field types in the classifier: `float` / `Float` as a magnitude,
  `Timestamp` / `datetime` as inert siblings of `Date`, and a bare integer
  (`int` / `Integer` / `BigInt`) accepted as opaque (inert) under the lenient
  default, the ambiguous case a future strict mode rejects (#73).

**Structural hazard detectors**

- The outer-join cluster under one effect x consumer x guard framing: `WHERE`
  inversion, the NULL-group-after-outer-join family (`GROUP BY`, join key,
  `NOT IN`), and a cross-model fan-out inflation detector with a symmetric
  join-key rule.
- Non-determinism detection, and snapshot reads missing a temporal filter.
- An upstream `not_null` test recommendation on `join_on_nullable_key`, naming
  why the key is nullable so the fix is actionable at its source.

**Cross-world analysis**

- One analysis door (`dblect.analysis.analyze`) over both detector families,
  returning every finding under one sealed type so a consumer cannot silently
  drop a family.
- Incremental-worlds checking: compile a model's full-refresh and steady-state
  forms data-free, run both detector families over each, and difference the
  findings so a hazard living only in the unexercised branch surfaces.
- Coverage reporting that separates resolution (lineage the propagator could
  follow) from grounding (columns a fact actually checks).

**CLI**

- `check` runs both detector families over a project, `init` scaffolds the
  declaration tree and writes model stubs (from `catalog.json` columns when it
  is present), `setup` installs the AI-assistant skill, and `version` prints the
  installed version.
- Manifest resolution that honors dbt's `target-path`, auto-discovers
  `target/manifest.json`, and falls back to running `dbt compile`, with
  actionable errors when neither a project nor a manifest is present and when
  dbt is not on `PATH`.
- `--base-manifest` reports only the findings a change introduces, by
  differencing the run against a base revision's manifest.
- Per-finding severity with a `--fail-on` threshold (default `warn`) driving the
  exit code, and a `--no-fail` override. A validated-adapter gate with
  `--dialect` as the operator's opt-in acknowledgment that detector behavior is
  best-effort off the validated set.

**Reporting and suppression**

- Text and JSON reporters under one versioned schema, plus SARIF 2.1.0 output
  for GitHub code scanning and similar surfaces. Status messages go to stderr so
  stdout is a clean report.
- A stable issue code on every finding, surfaced in the text and SARIF output.
- Compiled-SQL line spans back-mapped to their source positions, and
  macro-emitted findings anchored to the macro call site so a `-- noqa` there
  suppresses them.
- SQLFluff-compatible `-- noqa` suppression. A bare `-- noqa` silences every
  dblect finding on its line; a `-- noqa: DBLECT_<KIND>` directive silences one
  detector, and codes without the `DBLECT_` prefix are left for `dbt lint` so
  one comment can address both tools. dblect does not own the suppression
  grammar, so it coexists with dbt Fusion's `dbt lint`. A macro body's own
  `-- noqa` is honored through a compiled-frame directive scan, and every
  suppression is logged in the report's `suppressed:` section.

**Adapters**

- One `AdapterProfile` per warehouse behind a registry, gathering the
  dialect-specific facets the detectors need. DuckDB and BigQuery are validated
  end to end; profiles also ship for Postgres, Redshift, and Snowflake, reachable
  through `--dialect`.

**AI-assistant integration**

- The `dblect:bootstrap` skill and `dblect setup`, which install a
  drift-guarded, built-surface-only skill so an assistant can author
  declarations against the real API.

**Execution harness**

- A DuckDB execution harness that runs dbt models via subprocess with fixture
  overrides, the substrate the runtime layers will build on.

**Demo scenarios**

- A library of demo scenarios: developer-introduced bugs that `dblect check`
  catches.

[Unreleased]: https://github.com/dvryaboy/dblect/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dvryaboy/dblect/releases/tag/v0.1.0
