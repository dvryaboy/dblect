# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

A static analyzer for dbt projects that reads a compiled manifest, propagates
column-level facts through the DAG, and reports structural and
declaration-level findings the user can navigate to and act on. Nothing is
tagged yet; this section accumulates the work that a first release will carry.

### Added

**Manifest and SQL ingestion**

- Typed dbt manifest ingestion: `Manifest`, `Node`, and a `Dag` with topology,
  parsed from `manifest.json`.
- Analysis over each model's compiled SQL (not the raw Jinja), parsed once per
  run and shared across detectors via a sqlglot wrapper.
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

**Uniqueness analysis**

- Uniqueness facts from dbt declarations and from structural proof, an
  ordering-key detector and a join-fan-out detector over substrate keys, and
  propagation of uniqueness through SQL operations and across model
  dependencies.
- Candidate keys derived through surrogate-hash columns; seeds and snapshots
  spanned as test targets.

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

**Structural hazard detectors**

- Structural detectors over every model: outer-join `WHERE` inversion,
  non-determinism, the NULL-group-after-outer-join family (`GROUP BY`, join
  key, `NOT IN`), and snapshot reads missing a temporal filter.
- Source-line provenance on every finding, and `-- noqa-fixture:` suppression
  with a required reason.

**Cross-world analysis**

- One analysis door (`dblect.analysis.analyze`) over both detector families,
  returning every finding under one sealed type so a consumer cannot silently
  drop a family.
- Incremental-worlds checking: compile a model's full-refresh and steady-state
  forms data-free, run both detector families over each, and difference the
  findings so a hazard living only in the unexercised branch surfaces.
- Coverage reporting that separates resolution (lineage the propagator could
  follow) from grounding (columns a fact actually checks).

**CLI and reporting**

- The `dblect` CLI: `check` runs both detector families over a project, `init`
  scaffolds the declaration tree and writes model stubs, `version` prints the
  installed version.
- Text and JSON reporters under one versioned schema, with a non-zero exit on
  unsuppressed findings and a `--no-fail` override. Status messages go to
  stderr so stdout is a clean report.
- A SARIF 2.1.0 reporter (`--format sarif`) so findings render as pull-request
  annotations on GitHub code scanning and other SARIF-aware surfaces. Each
  finding becomes a result keyed by a family-namespaced rule id, suppressed
  findings carry their justification, and models the analysis could not read
  surface as notifications.
- A validated-adapter gate, with `--dialect` as the operator's opt-in
  acknowledgment that detector behavior is best-effort off the validated set.
- A library of demo scenarios: developer-introduced bugs that `dblect check`
  catches.

**Execution harness**

- A DuckDB execution harness that runs dbt models via subprocess with fixture
  overrides, the substrate the runtime layers will build on.

### Changed

- `aggregation_not_well_typed` findings now name what the coherence guard
  reasoned about: the aggregate and the column it reduced, the per-row companion
  that is not held constant, and the grouping that fails to hold it, instead of
  a generic message (#109).
- Aggregate behavior is now a first-class combine/select/count classification
  (`dblect.sql.aggregates`), the single source of truth for both arming the
  coherence guard and the not-well-typed finding. Keying on the sqlglot node type
  covers every dialect at once; `min`/`max` are classified as selecting aggregates
  and widen their result tag to top on a varying companion under the lenient
  default (#115).

### Fixed

- `count` (and `count_if`, `approx_count_distinct`) over a typed magnitude no
  longer raises a spurious `aggregation_not_well_typed` finding. Conversely,
  `stddev`, `variance`, `kurtosis`, `skewness`, `median`, `mode`, and the
  quantile/percentile family now correctly flag a mixed-currency reduction (#115).

[Unreleased]: https://github.com/dvryaboy/dblect/commits/main
