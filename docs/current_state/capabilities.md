# dblect: capabilities and the road to 0.1.0

A capability-by-capability ledger of what works end to end today, what is scaffolded but not yet load-bearing, and what a first usable release still needs. [architecture.md](./architecture.md) describes how the built pieces fit together; this document is the status view. The vision and the layered investment model live in [docs/dblect-overview.md](../dblect-overview.md) and [docs/design/tiers_and_rough_implementation_order.md](../design/tiers_and_rough_implementation_order.md).

## The shape of the project

The vision has two complementary halves: a **static** analyzer that reasons about the dbt DAG without running anything, and a **runtime** half that generates data and executes models to find value-level bugs. The static half is substantially built and works against real projects. The runtime half is designed but not built.

## What works end to end today

Run `dblect check <project>` and it does all of this in one pass, in seconds, with no execution and no LLM:

**Structural hazard detectors (zero declarations required).** Twelve checks over compiled SQL, each pinned to a file and line:

- ordering hazards: unordered ranking windows, unordered aggregates (`ARRAY_AGG`/`STRING_AGG` with no `ORDER BY`)
- join hazards: join fanout against declared/inferred keys, `COALESCE` on a join key, `WHERE` on an outer-joined nullable column (silent inner-join), join on a nullable key
- NULL hazards: `GROUP BY` collapsing unmatched rows into a NULL group (syntactic and lineage-aware variants), `NOT IN` over a nullable subquery
- determinism: non-deterministic builtins (`now()`, `random()`, ...) in load-bearing positions, adapter-aware
- window-key soundness: ranking windows whose order keys are not unique over their scope
- snapshot temporal-filter-missing

**The meaning-shift checker (the headline capability).** With domain types declared on the columns that matter, `dblect check` propagates types along the DAG and catches the canonical bug the project exists for: a column's semantic type flipping upstream (revenue net to gross, currency creep) so a downstream contract no longer holds. This is the `domain_type_contradiction` finding, and it fires at PR-review time with no data and no execution. The `aggregation_not_well_typed` finding catches the mixed-currency-sum class. Demonstrated end to end by the `currency_creep` scenario fixture.

**The supporting machinery, all built and tested:**

- the lineage substrate: one K-relations property propagator carrying where-provenance, nullability, uniqueness, functional-dependency, and domain-type properties over column- and relation-scoped graphs
- conditional facts: `where`-filtered dbt tests captured with their predicate and activated through a sound implication engine
- the domain-type DSL: `DomainType`, refinements, typed scalars, `ModelContract`, `@contract` methods over column proxies, and the fact bridge
- incremental worlds: an incremental model is compiled both ways (full-refresh and steady-state) and the detectors run over each, catching hazards that live only in the unexercised branch
- adapter profiles: per-warehouse dialect, write-enforcement, and hazardous-builtin knowledge behind a registry
- `dblect init`: scaffolds the `dblect/` tree and generates editor stubs from the manifest
- `dblect check`: text and JSON (documented schema) reporters, coverage reporting, SQLFluff-compatible `-- noqa` suppression (bare `-- noqa` silences every finding on a line; `-- noqa: DBLECT_<KIND>` silences one detector and coexists with `dbt lint`), a resolution floor so thin coverage cannot read as a clean bill
- catalog.json ingestion for seed/source leaf columns; nested-field (STRUCT) lineage
- var/env_var discovery from source Jinja (`varinf`)

822 tests pass, including property-based tests for DAG topology, semiring laws, lineage propagation, and an execution-oracle soundness PBT for uniqueness. Strict pyright and ruff in CI.

## Scaffolded but not yet load-bearing

- **Flag/config worlds (the multi-world-under-flags analysis).** The engine is real and tested: `enumerate_worlds` / `check_worlds` lower a set of compile-time flags into worlds, run the properties per world, and report per-world findings and coverage. But it is **dark** today in two senses. It takes **hand-authored `DomainFlag`s** rather than flags discovered from the project, and it is **not wired into the `dblect check` CLI or the analysis door** — the check path runs the base world only (`worlds: 1 (base)`). The Jinja front end for var discovery has landed (`varinf`, PR #104) and the manifest macro registry is in place (#103), but the rest of the pipeline that would light this up on a real project (macro-following, the control-flow-vs-value-substitution classifier, domain inference, `dblect scaffold flags`, and the CLI wiring that reads the scaffolded flag file) is still ahead. See issue #98 and the design under `docs/design/var-inference/` (PR #102, design-only). Per-contract world scoping (#99) and control-flow world evaluation (#100) sit on top of it. Flag-flip preflight depends on the whole stack.
- **The DuckDB execution harness.** `execution/run.py` runs dbt models against ephemeral DuckDB and reads results back. It powers the incremental-world compiler and tests, but is **not on the `dblect check` path**. It is the substrate the runtime layer will sit on.
- **Contract predicates.** `@contract` method bodies are collected and counted, not executed. Verifying them needs materialized data.

## Not built: the runtime half

This is genuinely greenfield and is the larger half of the original vision:

- property-based execution: type-driven generators (Hegel/Hypothesis), structural PBT, heuristic invariants (row-count sanity, PK uniqueness, monotonicity, conservation)
- replay-determinism via differential execution (run N times, diff under equivalence)
- the generator framework: contract-directed generation, the intent catalog, multi-table coordinated FK-respecting generation, domain-aware shrinking. This is the single largest body of remaining work and where most of the implementation difficulty lives.
- change-impact analysis and flag-flip preflight at PR time
- the MCP server, HTML/SARIF reporting, `dblect show-case`

## Distance to a usable 0.1.0

The runtime half is months of work gated on the coordinated-generation crux, so it should not gate 0.1.0. That leaves a real scoping fork on the static side, turning on whether the **multi-world-under-flags analysis** is part of the basic vision the first release delivers.

**Fork A: base-world static analyzer.** Single-world (base) analysis: the meaning-shift checker plus the dozen zero-declaration hazard detectors, packaged and hardened, with flag-worlds deferred. This runs in seconds with no execution and no LLM, so per-run cost is negligible, and it is independent of the var-inference work. It still needs:

1. **A second validated warehouse adapter (Snowflake or BigQuery).** Today only `duckdb` is validated end to end; every real project on another warehouse trips the best-effort warning. This is the biggest real-world adoption blocker, because the target audience runs Snowflake/BigQuery, not DuckDB.
2. **CI ergonomics.** SARIF output (#9), `--diff <base-ref>` to limit findings to changed lines (#10), and severity with a tunable fail threshold (#11). These are what let a team adopt it in CI without drowning in pre-existing findings on day one.
3. **Source-line mapping.** Findings currently point at compiled-SQL line numbers; for macro-heavy models these diverge from the `.sql` the developer wrote. Back-mapping to source positions makes findings actionable.
4. **Suppression for declaration findings** (#117), and a real-project shakedown beyond the jaffle and scenario fixtures to calibrate false-positive rates.
5. **Packaging and distribution:** `pip install dblect`, a quickstart, and the `init` to first-finding path validated on an external project.

None of these is a new subsystem; the work is breadth and hardening. Fork A is realistically **weeks of focused work**, dominated by item 1 and item 2.

**Fork B: static analyzer with flag-world analysis.** If the powerful multi-world-under-flags analysis is a headline capability of the first release (and it is one of the more differentiated parts of the vision), then **var-inference (#98) is on the critical path**, not a deferrable polish item. The world engine already exists, but it is unusable on a real project until the project's flags can be *discovered and scaffolded* rather than hand-declared: a real project has hundreds of vars, most of which are not world axes, so the control-flow classifier is what makes enumeration tractable. Landing this means everything past the Jinja front end:

- macro-following (expansion, cycle detection, symbolic conditional eval)
- the three-way classifier (control-flow vs value-substitution vs computed) and domain/type inference
- `dblect scaffold flags` (generate the `DomainFlag` file, with the user filling in the `affects` clause static analysis cannot infer)
- wiring `check_worlds` into the `dblect check` CLI so it reads the scaffolded flag surface and reports per-world findings

Today that pipeline is design-only (PR #102) past the discovery front end. It is a genuine body of work — call it the long pole of the static side, on the order of the original facts-substrate or domain-type efforts, not a few-day task. Fork B is therefore **meaningfully longer than Fork A**, plausibly the dominant cost of the release.

**Recommendation.** Ship Fork A as 0.1.0 to get value in front of real projects quickly and cheaply, and treat flag-world analysis (Fork B) as the headline of a fast-following 0.1.x or 0.2.0 once var-inference lands. The base-world analyzer already delivers the meaning-shift capability the project exists for; flag-worlds deepen it but are not required for the first useful release. If instead flag-world analysis is considered non-negotiable for 0.1.0, plan the release around var-inference as its critical path and size it accordingly. The runtime half remains the 0.2.0+ arc in either case.
