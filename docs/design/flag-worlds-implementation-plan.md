# Flag worlds: implementation plan

Status: plan
Audience: engineers picking up the config-and-flag-worlds work. It assumes [`config-and-flag-worlds.md`](./config-and-flag-worlds.md) (the design and the strategy fork), [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md) (the user-facing surface), and [`var-inference-spec.md`](./var-inference-spec.md) (the discovery layer). This doc turns those into an ordered build with the decisions that have to be made before code lands.

## Where the substrate already is

The design leans on substrate that is in place and exercised today, which is why the plan starts close to shipping rather than at foundations. Confirmed against the code:

- **World-indexed facts.** `WorldRef`, `CompileOrigin`, and `CompileValue` exist in [`facts/model.py`](../../src/dblect/lineage/facts/model.py). `BASE_WORLD` is live: every fact today carries it, so resolution is the ordinary single-world fold and a non-empty world simply changes the bucket key.
- **The config discoverer shipped.** `_ConfigKeyDiscoverer` / `config_key_discoverer` in [`properties/uniqueness.py`](../../src/dblect/lineage/properties/uniqueness.py) reads `node.config` (`materialized`, `unique_key`, `incremental_strategy`), honors the dedup-strategy set, and emits a `CompileValue(origin=DBT_CONFIG, world=BASE_WORLD)` candidate-key fact.
- **Both provenance flavors.** `where_provenance.py` carries value-feeding source columns; the semiring framework in [`semiring.py`](../../src/dblect/lineage/semiring.py) plus the uniqueness property and `NullabilitySemiring` carry the multiplicity (how-provenance) side. These are the substrates the per-property influence cone is built from.
- **Nullability flow** is mature in [`properties/nullability.py`](../../src/dblect/lineage/properties/nullability.py): lattice, outer-join taint, conditional activation, detectors.
- **The predicate-implication engine** ([`predicate.py`](../../src/dblect/lineage/predicate.py)) is complete and sound, already used for conditional-fact activation.

The gap is not the substrate. It is flag discovery (var-inference is spec-only, no code) and the world enumerator that turns one analysis into a per-world one. `run_check` ([`check/run.py`](../../src/dblect/check/run.py)) is single-manifest, single-world end to end.

## Four work streams

The streams are ordered so each ships something correct and the expensive, speculative machinery is gated behind demonstrated need. Only the first depends on nothing new.

### World plumbing and the fact-level enumerator

The start-now stream. It stages the analysis for many worlds and ships the world-keyed plumbing and coverage independent of var-inference, making `WorldRef` real beyond `BASE_WORLD` for the first time.

- **Stage `run_check` into separable steps.** Today `run_check` builds the graphs, grounds facts, and propagates in one pass. Separate graph build (per compiled SQL) from fact grounding (per world) from propagation, so the enumerator can hold one graph build and re-propagate under per-world facts. This is the load-bearing refactor; D1 commits us to it.
- **A fact-level enumerator.** One shared graph build; the enumerator varies the `CompileValue` facts per world and re-propagates, aggregating findings keyed by `WorldRef`. This is the mechanism that scales to monster projects, where compiling and rebuilding a manifest per world is infeasible.
- **Graph patching for the always-present control-flow axes.** `is_incremental()` and `target` change the SQL, so pure fact substitution over one graph cannot represent them. Rather than recompiling the whole project per world, compile only the affected models in their alternate world (deduplicated, model-scoped via `dbt compile --select`) and patch their subgraphs into the shared graph. This keeps the always-present axes reachable without the manifest-per-world product.
- **World coverage reporting.** Land the coverage output from the design's "Coverage as a first-class output" section early: how many worlds a contract was checked under, which axes were enumerated, which were left at the base world. This turns today's one-world blindness into a measured number.
- **Exit criteria.** The enumerator runs the staged analysis across a small set of worlds over one shared graph and reports findings keyed by world, with coverage stating which axes were swept and which stayed at the base world.

### Var-inference

The long pole, and the critical-path dependency for everything flag-driven. It is specified in [`var-inference-spec.md`](./var-inference-spec.md) and has no code yet, so it is its own milestone.

- Discover `var()` and `env_var()` usage from Jinja source, infer type, domain, and default, and detect numeric branch points (the spec's `UsageContext` variants).
- Produce the scaffold (`dblect/flags/discovered.py`) and the discovery report.
- Emit per-model var usage, which is the exact input the bridge's model-responsiveness rule consumes.
- **Exit criteria.** `dblect scaffold flags` produces a reviewable `DomainFlag` scaffold for a real project, with per-model var usage recorded. The config discoverer is the template for the fact-emission shape.

### The flag-world bridge

The `#40` work: given a chosen world, lower flag declarations to `CompileValue` facts. Depends on var-inference for domain, type, and usage.

- **Lower `affects` to a fact.** Given a `WorldRef` fixing a flag's value, evaluate `affects` to one axis value and emit a concrete `CompileValue(origin=DBT_VAR|ENV_VAR)` at each responsive scope. The disjunction across values lives in the enumerator, never in the substrate.
- **Model-responsiveness grounding.** A scope declared as the target type, in a model that reads the controlling var, gets the fact. This is the design's recommended v1 reading; it needs only the per-model var usage var-inference already produces, and degrades to silence rather than a wrong fact.
- **The `COMPUTED` and inline-default cases.** A flag whose value is not statically enumerable becomes one honest world (the manifest's resolved value), recorded in coverage as un-enumerated.
- **Exercisable ahead of var-inference.** A hand-declared `DomainFlag` (explicit domain, type-directed or declared responsiveness) drives the bridge and the fact-level enumerator without waiting on the long pole, which gives the first value-substitution cross-world finding early. Var-inference then automates discovery so users stop hand-declaring.
- **Exit criteria.** The first value-substitution cross-world check: a downstream contract pinning a refinement axis passes in one flag world and fails in another, with the world named.

### Factoring, then lifting

Gated by the empirical fork in [`config-and-flag-worlds.md`](./config-and-flag-worlds.md). These do not block the streams above and should not be in the early plan.

- **Influence-cone factoring and interaction clusters.** Build the per-property flag-influence set from the appropriate provenance (Decision D3), cluster by transitive closure, and enumerate the disjoint union of per-cluster products. Justified when a project's flag count makes the unfactored world space the bottleneck.
- **Variability-aware Jinja front end.** The lifted graph carries the choice nodes, so it is both the scale endgame and, under the fact-level architecture, the general mechanism for `{% if var %}` control-flow worlds beyond the always-present axes that graph patching covers. How far to build it is gated by the fork's seams; that it is on the path for general control-flow coverage is a consequence of rejecting manifest-per-world recompilation (D1).

## Decisions, resolved

These three ripple through types and interfaces, so they were settled before the code commits to them. dblect is not yet in production use, so none of them is constrained by back-compatibility: fact types and interfaces change freely to take the clean design. Context is kept for each so the rationale survives.

### D1. Resolved: fact-level enumerator, with `run_check` staged

**Decision.** Fact-level. The enumerator holds one shared graph build and varies `CompileValue` facts per world, re-propagating. Manifest-per-world recompilation is rejected as the general mechanism, because the target projects are monster-scale, where compiling and rebuilding a manifest per world does not scale. `run_check` is staged into graph-build / fact-grounding / propagate as part of the first stream.

**Context and consequence.** `run_check` is single-world end to end today: it builds the relation and column graphs from one manifest, grounds facts, propagates, and collects findings in one function. Fact-level enumeration is the design's "shared walk, only the leaves vary" vision, and it is the only form that scales when the manifest is large. The consequence, accepted: control-flow worlds, where the SQL itself differs, cannot be represented by fact substitution over a single graph. Value-substitution worlds are handled directly. The always-present control-flow axes (`is_incremental()`, `target`) are reached by model-scoped, deduplicated recompilation patched into the shared graph rather than by whole-manifest recompilation. General `{% if var %}` control-flow worlds route through the variability-aware front end, whose lifted graph carries the choice nodes; rejecting manifest-per-world recompilation is what puts that front end on the path for full control-flow coverage. Bounded recompilation survives only in its targeted, deduplicated, graph-patching form, never as the manifest-per-world product.

### D2. Resolved: enforcement is a materialization property, snapshots first-class, hazard is its own detector

**Decision.** The candidate-key fact carries enforcement standing, and enforcement is computed as a property of the *materialization* rather than hardcoded to the incremental dedup strategies. The enforcing materializations are incremental `merge`-with-key and `delete+insert`, and **snapshots**, which enforce uniqueness on their key and are a first-class enforcing case; snapshot support lands in [#52](https://github.com/dvryaboy/dblect/issues/52). The single-batch duplicate hazard is a separate detector.

**Context.** A `merge`-with-key materialization enforces uniqueness at write time, a stronger guarantee than a `unique` data test (the test asserts the SELECT is unique; the materialization makes the table unique regardless of the SELECT). `NativeConstraint` already carries `enforced_on_write` ([`facts/model.py`](../../src/dblect/lineage/facts/model.py)); the candidate-key fact gains the analogous standing. Snapshots are the case that forces enforcement to be a materialization property rather than an incremental-strategy check: they enforce uniqueness without being incremental, so a merge-or-`delete+insert` test would miss them. The config discoverer accordingly stops asking "is this incremental with a dedup strategy" and starts asking "does this materialization enforce uniqueness," with the enforcing set extensible as new enforcing materializations (snapshots, and whatever follows) are recognized. The guarantee is not unconditional: `merge` deduplicates incoming rows against existing rows, but a single batch containing duplicate keys is adapter-dependent. That hazard is the relation-level analog of join fan-out and rides as its own detector, keeping the key fact unconditional and the hazard a legible separate finding.

### D3. Resolved: dedicated multiplicity provenance

**Decision.** Build a dedicated per-node multiplicity provenance, a semiring property recording the multiplicity-affecting operators and flags upstream of a node, and build the uniqueness and cardinality influence cone from it precisely, rather than approximating from where-provenance and the relation graph.

**Context.** The per-property influence cone uses where-provenance for value and domain contracts, and how-provenance (multiplicity) for uniqueness and cardinality. Where-provenance is a per-column `frozenset[ColumnRef]` today; there is no equally direct per-node "which upstream operators and flags can change this node's multiplicity" query. An approximation from where-provenance plus the relation graph would be sound but over-cluster, costing worlds. The dedicated provenance costs a new property and its tests, and buys an exact cone: uniqueness and cardinality contracts pay only for the flags that genuinely move their multiplicity, with no over-clustering. This is the precision the design's per-property cone is meant to deliver, made exact rather than approximate.

### A note on world output, deferred by default

The design's ergonomics section offers two output forms: per-world enumeration ("world: include_tax=false -> FAIL") and a presence-condition or decision-tree form that reports the condition under which a contract fails. Per-world enumeration is right for the handful of worlds the early streams produce. The decision-tree form is a readability investment for large spaces, gated by the same empirical fork that gates factoring, so it is deferred rather than decided now.

## Critical path

The config discoverer already ships. The world-plumbing stream can start immediately: it stages `run_check`, builds the fact-level enumerator, and lands world-keyed coverage, with no new dependencies. The first cross-world *finding* rides the bridge, which a hand-declared flag can exercise ahead of var-inference, so it does not wait on the long pole. Var-inference is the long pole and automates flag discovery, after which the bridge runs without hand-declaration. The always-present control-flow axes ride model-scoped graph patching; general `{% if var %}` control-flow coverage rides the variability-aware front end, which D1 places on the path. Factoring and the front end are gated by measurements against real projects, which the fork's five seams make readable from compiled SQL and lineage before either is built. D1, D2, and D3 are settled above, so the streams can commit to the fact-level interface, the materialization-derived enforcement, and the dedicated multiplicity provenance directly.

## What this plan does not cover

- The internals of the world enumerator's diffing and reporting beyond the interface D1 fixes.
- The variability-aware Jinja front end's construction, which is specified when it is scheduled.
- The discovery adapters deferred in [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md) (per-entity flags, external flag platforms, cross-package inference).

## File-level breakdown: world plumbing and the fact-level enumerator

This is the first stream specified to the file. It is a working scratch for the implementer and will be removed before merge; the durable record is the streams and decisions above.

### Staging `run_check` (in [`check/run.py`](../../src/dblect/check/run.py))

Today `run_check` resolves contracts, builds the relation and column graphs, propagates FD then domain type in `_propagate`, and derives findings, in one pass. Split the world-invariant build from the per-world propagation:

- `CheckGraphs` (frozen): the world-invariant build, holding `manifest`, `resolved` contracts, and both graph builds. The graph builds stamp `SourceRef`/`ColumnRef` onto the parsed trees in place; those stamps are world-invariant and `propagate` never mutates the trees, so one build is safe to reuse across worlds. State this as an invariant in the docstring so a later control-flow author does not mutate the shared trees.
- `build_check_graphs(manifest, *, registry=None, dialect="duckdb") -> CheckGraphs`. `resolve_contracts` reads only manifest and registry, so it belongs here.
- `WorldFacts` (frozen): `world: WorldRef`, plus the world-invariant declared facts and this world's `CompileValue` leaves, kept in separate tuples so the enumerator only appends compile facts and never recomputes declared ones.
- `base_world_facts(resolved) -> WorldFacts`: wraps `resolved.fd_facts`/`tag_facts` under `BASE_WORLD`, reproducing today's facts exactly.
- `propagate_world(graphs, facts) -> WorldAnnotations`: the verbatim body of `_propagate`, grounded from `facts` rather than `resolved`, with a fresh `AnnotationStore` per call. `WorldAnnotations` is a frozen bundle (`world`, `domain_type` map) so a later property can be added without changing the signature.
- `run_check` becomes the thin orchestrator: build graphs, `base_world_facts`, `propagate_world`, derive findings (`_issue_findings` once, `_contradiction_findings`/`_aggregation_findings` over the one world). Signature and behavior unchanged; the CLI call site in [`cli/__init__.py`](../../src/dblect/cli/__init__.py) does not change.

The single-world path stays identical because `base_world_facts` hands `propagate_world` exactly today's fact tuples and `propagate_world` is today's `_propagate`. Pin it with a staging-equivalence boundary test and the unchanged `tests/check/test_run_check.py`.

### The enumerator (new [`check/worlds.py`](../../src/dblect/check/worlds.py))

- `enumerate_worlds(graphs, world_facts: Mapping[WorldRef, tuple[CompileFact, ...]]) -> EnumeratedFindings`: holds one `CheckGraphs`; for each world, builds `WorldFacts` (shared declared facts + this world's routed compile facts), calls `propagate_world`, derives findings, collects a `WorldResult` keyed by `WorldRef`. `BASE_WORLD` with an empty compile-fact tuple reproduces `run_check`.
- `CompileFact` is a tagged container (property tag + the typed `Fact`) so one map carries both `DomainTag` and `FDSet` facts without stringy typing.
- Cross-world disagreement is data: a finding present in some worlds and absent in others becomes distinct `WorldResult`s, grouped by finding identity in `EnumeratedFindings`. No exception on disagreement.
- For this stream the worlds and their facts are supplied directly (a hand-declared `DomainFlag` lowered by hand, or a fixture). The enumerator does not cache annotations across worlds; the lifted/shared-representation optimization is the deferred factoring stream, and the docstring says so to prevent premature coupling.

Re-running propagation per world is clean: `AnnotationStore` and the `propagate` memo are per-call, grounding folds by lattice meet and ignores provenance, and the graphs are immutable. So bucketing by world is filtering which facts reach grounding, never touching `resolve`.

### Coverage (in [`check/findings.py`](../../src/dblect/check/findings.py) and [`check/report.py`](../../src/dblect/check/report.py))

- `ContractCoverage` (`contract`, `worlds_checked`, `axes_at_base_world`) and `WorldCoverage` (`worlds_enumerated`, `per_contract`), added to `CheckReport`. The single-world `run_check` produces the trivial value (one base world, every axis at base).
- This stream reports enumerated worlds, not influence-scoped worlds; precise per-contract flag-influence attribution waits for the multiplicity cone. The docstring says so, so the number is honest about what it measures.
- `render_text` gains an additive coverage line (one honest line for the single-world case, so the existing summary-line assertions hold). `render_json` gains a `coverage` block and bumps `JSON_SCHEMA_VERSION` to `"2"`; the schema-version test updates for the new shape.

### Commit sequencing

1. **Stage `run_check`, behavior-preserving.** Add the staging types and functions, rewrite `run_check` to orchestrate them, export from `check/__init__.py`. Staging-equivalence test lands; `test_run_check.py` and the propagation suite stay green unchanged.
2. **Add the enumerator.** New `check/worlds.py`; base-world-identity, determinism, and cross-world-disagreement tests. Additive, behind a new entry point.
3. **Land coverage.** Coverage types on `CheckReport`, both renderers, the single JSON schema-version bump and its test update.

`BASE_WORLD`, the config discoverer, and `CompileValue` construction already exist on `main`, so no substrate commit precedes these.
