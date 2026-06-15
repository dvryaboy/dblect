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

### World plumbing and bounded re-compilation

The start-now stream. It needs no var-inference and delivers value immediately, while making `WorldRef` real beyond `BASE_WORLD` for the first time.

- **Stage `run_check` so worlds can be enumerated over it.** Today `run_check` builds the graphs, grounds facts, and propagates in one pass. Separate the stages so a caller can drive many worlds: graph build (per compiled manifest), fact grounding (per world), propagate, collect findings. Decision D1 fixes how far this separation goes.
- **A minimal world enumerator.** Produce the worlds for the always-present control-flow axes (`is_incremental()` has two states; `target` has a small closed set), compile each via the existing `dbt compile` path in `_resolve_manifest_path` ([`cli/__init__.py`](../../src/dblect/cli/__init__.py)), run the staged analysis per world, and aggregate findings keyed by `WorldRef`.
- **World coverage reporting.** Land the coverage output from the design's "Coverage as a first-class output" section early: how many worlds a contract was checked under, which axes were enumerated, which were left at the single manifest. This turns today's one-world blindness into a measured number.
- **Exit criteria.** `dblect check` reports a contract that holds under `is_incremental()=false` and fails under `true` (or across targets), with the failing world named, and the coverage report states which axes it swept.

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
- **Exit criteria.** The first value-substitution cross-world check: a downstream contract pinning a refinement axis passes in one flag world and fails in another, with the world named.

### Factoring, then lifting

Gated by the empirical fork in [`config-and-flag-worlds.md`](./config-and-flag-worlds.md). These do not block the streams above and should not be in the early plan.

- **Influence-cone factoring and interaction clusters.** Build the per-property flag-influence set from the appropriate provenance (Decision D3), cluster by transitive closure, and enumerate the disjoint union of per-cluster products. Justified when a project's flag count makes the unfactored world space the bottleneck.
- **Variability-aware Jinja front end.** The lifted endgame. Justified only for the tail of internally-variable models the fork's seams identify, never on spec.

## Decisions to make before the code commits to them

Each of these ripples through types or interfaces, so they are worth settling deliberately. For each: the context, the options, and what each option commits us to.

### D1. The world enumerator interface, and how far to stage `run_check`

**Context.** `run_check` is single-world end to end: it builds the relation and column graphs from one manifest, grounds facts, propagates, and collects findings in one function. Every cross-world feature needs something that produces N worlds, runs the analysis in each, and aggregates findings keyed by world. The design deliberately scopes the enumerator out, but the first shippable stream cannot ship without a minimal one, so its shape is the first thing to fix. The choice is how the enumerator relates to `run_check`, and it sets the architecture the later value-substitution and lifted work inherit.

**Options.**

- **Manifest-level enumerator.** A world is a compiled manifest. The enumerator produces one manifest per world (via `dbt compile` for control-flow axes), calls `run_check` per manifest unchanged, and diffs the `CheckReport`s. Simplest, fully sound, reuses the whole pipeline.
- **Fact-level enumerator.** One manifest, one graph build, and the enumerator varies only the `CompileValue` facts per world, re-propagating over a shared graph. This is the design's "shared walk, only the leaves vary" vision for value-substitution worlds, where the SQL is identical across worlds and only the grounded values differ.
- **Hybrid.** Manifest-level for control-flow worlds (they change the SQL, so they must recompile) and fact-level for value-substitution worlds (the SQL is fixed). This matches the world taxonomy in the design.

**What each commits us to.** Manifest-level is the fastest path to the first stream and is the correct treatment for the bounded-recompile axes, which genuinely change the SQL and must be recompiled regardless. Its cost is that it bakes in "a world is a manifest," which makes value-substitution worlds recompile when they need not and forgoes the shared-walk payoff the design promises. Fact-level commits us up front to a `run_check` refactor that separates graph build from fact grounding from propagation, so worlds can share the graph; it is more initial work but it is the architecture both value-substitution and the lifted endgame need. Hybrid is the design-consistent destination and the largest interface surface.

**Recommendation to weigh.** Build the manifest-level enumerator for the bounded-recompile stream now, since those axes must recompile anyway, but stage `run_check` into separable graph-build / fact-grounding / propagate steps as part of that work. That staging is cheap to do now, keeps the first stream simple, and lets the fact-level path slot in for value-substitution worlds later without a rewrite. The decision for you is whether to pay for that staging now or accept a later refactor when value-substitution worlds arrive.

### D2. Config-derived keys: enforced or asserted, and where the single-batch hazard rides

**Context.** This is the design's first open question, and it concerns live shipping code. The config discoverer emits a candidate-key fact from `unique_key` x `incremental_strategy`. A `merge`-with-key materialization enforces uniqueness at write time, which is a stronger guarantee than a `unique` data test (the test asserts the SELECT is unique; `merge` makes the table unique regardless of the SELECT). `NativeConstraint` already carries `enforced_on_write` ([`facts/model.py`](../../src/dblect/lineage/facts/model.py)); `CompileValue` does not. The guarantee is not unconditional: `merge` deduplicates incoming rows against existing rows, but a single batch containing duplicate keys is adapter-dependent (some error, some pick arbitrarily).

**Options.**

- **Enforcement standing.** Treat the key as a plain candidate key (status quo), or give the fact an enforcement flag analogous to `NativeConstraint.enforced_on_write` so downstream can distinguish an enforced key from an asserted one.
- **The single-batch hazard.** Ride it as a conditional caveat on the same key fact, or surface it as a wholly separate detector (a SELECT that can produce duplicate keys feeding a `merge`, which is the relation-level analog of join fan-out).

**What each commits us to.** A plain candidate key keeps the fact type simple but under-uses the strongest signal config gives us: downstream uniqueness reasoning cannot tell "enforced at write, holds across runs" from "someone asserted this." An enforcement flag commits us to threading one bit through the uniqueness fact type and teaching the relevant findings to read it, after which a config key can carry near-constraint standing. Riding the hazard as a caveat on the fact keeps everything in one place but muddies an otherwise-clean enforced fact with a conditional; a separate detector keeps the key fact unconditional and makes the single-batch hazard its own finding with its own explanation, consistent with how join fan-out is handled.

**Recommendation to weigh.** Add the enforcement flag and make the single-batch hazard a separate detector. This gives the enforced key its full standing and keeps the hazard legible as the relation-level fan-out analog. The decision is yours because it changes a shipping fact type and sets precedent for how config-derived guarantees carry enforcement.

### D3. The cone for multiplicity properties: approximate from existing graphs, or build a multiplicity provenance

**Context.** Relevant only to the factoring stream, but it scopes a claim the design now makes. The per-property influence cone uses where-provenance for value and domain contracts, and how-provenance (multiplicity) for uniqueness and cardinality. Where-provenance is a per-column `frozenset[ColumnRef]` today; there is no equally direct per-node "which upstream operators and flags can change this node's multiplicity" query. The semiring framework and the uniqueness property carry the multiplicity reasoning, but the cone for a uniqueness contract has to name the operators that move multiplicity (joins, group-bys, `distinct`, the config dedup), which live in the relation graph rather than in where-provenance.

**Options.**

- **Approximate the multiplicity cone from existing structure.** Derive the uniqueness and cardinality cone from where-provenance plus the relation lineage graph (joins and group-bys are visible there) plus the config dedup facts, accepting that it over-approximates. Over-approximation over-clusters, which costs worlds, never soundness.
- **Build a dedicated multiplicity provenance.** A new per-node semiring property recording the multiplicity-affecting operators upstream, queried to build the cone precisely.

**What each commits us to.** The approximation commits us to "the cone is sound but over-approximate, derived from graphs we already build," which is consistent with the design's stance that over-clustering is acceptable and only soundness is sacred, and it adds no new substrate. A dedicated provenance commits us to new property code and its tests, buying tighter clusters where the approximation over-clusters.

**Recommendation to weigh.** Approximate from the existing where-provenance and relation graph for v1, and treat an exact multiplicity provenance as a refinement the cluster-size measurements justify or do not. This keeps the design's "substrate already exists" claim honest: the substrate exists to build a sound cone, and tightening it is a later, measured choice.

### A note on world output, deferred by default

The design's ergonomics section offers two output forms: per-world enumeration ("world: include_tax=false -> FAIL") and a presence-condition or decision-tree form that reports the condition under which a contract fails. Per-world enumeration is right for the handful of worlds the early streams produce. The decision-tree form is a readability investment for large spaces, gated by the same empirical fork that gates factoring, so it is deferred rather than decided now.

## Critical path

The config discoverer already ships. The world-plumbing stream can start immediately and ships the first cross-world checks for the always-present axes with no new dependencies. Var-inference is the long pole and gates the bridge, which delivers the first var-driven cross-world check. Factoring and lifting are gated by measurements against real projects, which the fork's five seams make readable from compiled SQL and lineage before either is built. D1 and D2 are worth settling before the world-plumbing stream commits to an interface and a fact type; D3 can wait for the factoring stream.

## What this plan does not cover

- The internals of the world enumerator's diffing and reporting beyond the interface D1 fixes.
- The variability-aware Jinja front end's construction, which is specified when it is scheduled.
- The discovery adapters deferred in [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md) (per-entity flags, external flag platforms, cross-package inference).
