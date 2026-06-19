# Incremental worlds: checking incremental models in both compilations

Status: implemented (global two-world default). The mixed-state and cone refinements remain forward-looking.
Audience: engineers working on the world-compiler and the incremental check. It builds on the world theory in [`config-and-flag-worlds.md`](./config-and-flag-worlds.md) (how a configuration becomes facts and what it means to check across more than one world) and the execution substrate in [`src/dblect/execution/run.py`](../../src/dblect/execution/run.py). The as-built code is [`src/dblect/execution/incremental.py`](../../src/dblect/execution/incremental.py) (the world-compiler) and [`src/dblect/check/incremental.py`](../../src/dblect/check/incremental.py) (the per-world check and cross-world diff).

A dbt incremental model compiles to two different SQL statements: a first-run / full-refresh form that builds over all rows, and a steady-state form whose `{% if is_incremental() %}` branch is present. The dbt docs require both to be valid, so both are reachable by construction, yet a single manifest captures exactly one. This stream compiles a project both ways and runs the project's detectors over each: both the declaration-level checker (`run_check`) and the SQL-structural audit (`run_audit`). A finding that holds in one world and breaks in the other becomes a cross-world finding instead of a blind spot. The hazard this most wants to catch lives in the structural family, where a key the full-refresh build keeps unique is fanned out by a steady-state-only join.

This is the first of the always-present control-flow axes named in [`config-and-flag-worlds.md`](./config-and-flag-worlds.md). It is deliberately scoped ahead of the rest of the var-inference layer because it applies to any project with an incremental model, asks the developer for nothing, and rests on machinery that already exists.

## Where this sits

[`config-and-flag-worlds.md`](./config-and-flag-worlds.md) names the gap precisely: the current static analyzer is sound and useful, and it analyzes one compilation of every model that branches on configuration. For an incremental model that compilation is whichever branch dbt last produced, so any hazard (or any clean bill) in the unexercised branch is invisible. That doc also names the remedy and its cost: `is_incremental()` "has exactly two states ... so even before a general enumerator exists, compiling those specific worlds (two for incremental ...) closes the highest-frequency control-flow gap at a fixed, small cost."

Two pieces this stream leans on are already built. The config discoverer ([#39](https://github.com/dvryaboy/dblect/issues/39)) reads `materialized` and `incremental_strategy` from `node.config` into a typed `ModelConfig`, which tells us which models are incremental. The execution substrate ([`run.py`](../../src/dblect/execution/run.py)) already copies a project to a temp tree, writes a `profiles.yml` against an ephemeral DuckDB, and invokes dbt; the world-compiler reuses that setup.

These are control-flow worlds: the SQL itself differs between full-refresh and steady-state. That sets them apart from the value-substitution worlds the fact-level enumerator (`enumerate_worlds` in `check/worlds.py`) serves, where one shared build is re-grounded per world because the SQL is identical and only grounded values differ. A control-flow world cannot share a build, so each world here is compiled and analyzed independently from its own `Manifest`, and the cross-world diff (below) reconciles the two finding sets.

## The world model: one global run-mode axis

The axis is the project's **run mode**, with two values: full-refresh (every incremental model takes its relation-absent branch) and steady-state (every incremental model takes its `is_incremental()` branch). The project is compiled once under each, giving two project-level worlds.

This is a global axis, not a per-model one, and that choice is load-bearing. Treating each incremental model as an independent binary axis would enumerate `2^N` worlds for `N` incremental models, which is unaffordable on a real project. The global framing gives two worlds regardless of `N`. It is also the operationally faithful reading: a dbt invocation puts the whole project in one mode. A normal `dbt run` builds every incremental model in steady-state, and `dbt run --full-refresh` rebuilds every one from scratch. Running one model incremental while another full-refreshes in the same logical run is the exception (a selective `--full-refresh`, a newly added model backfilling, a dropped relation), not the common case. Those mixed states are real and worth checking eventually; they are handled as a refinement rather than as the default, described under [Keeping the axis cheap](#keeping-the-axis-cheap-and-the-mixed-state-refinement).

The axis is present for a project exactly when at least one model is incremental-materialized, read from the `ModelConfig` the config discoverer already produces. A project with no incremental models has one world and this stream is a no-op for it.

## Obtaining both worlds

Both worlds come from `dbt compile` alone, with no build and no warehouse data. The lever is that `is_incremental()` is a macro, and a root-project macro of the same name shadows dbt's built-in for the bare `{{ is_incremental() }}` call that incremental models use. The world-compiler injects an override whose body is a constant for the world being compiled:

```jinja
{# steady-state world #}   {% macro is_incremental() %}{{ return(true) }}{% endmacro %}
{# full-refresh world #}   {% macro is_incremental() %}{{ return(false) }}{% endmacro %}
```

Writing the `true` body yields the steady-state world and the `false` body the full-refresh world. Because the override returns the value directly, neither compile runs an introspective query or depends on a relation existing: `ref()` and `{{ this }}` resolve to relation names at parse, so the steady-state SELECT compiles even though nothing has been built. The override forces every incremental model the same way in one compile, which is exactly the global run-mode world the [world model](#the-world-model-one-global-run-mode-axis) describes. The override body carries no `var()` or `env_var()`, so it is inert to var discovery and reserves no var name (see [Relationship to var support](#relationship-to-var-support)).

Two probes established this. The first confirmed the underlying dbt behavior: compiling a model twice against a persistent DuckDB produced the relation-absent SELECT and then, once the relation existed, the same SELECT with its watermark branch (`where event_time > (select max(event_time) from <this>)`). The second confirmed the path the stream adopts: with a constant `is_incremental()` override in place and no seed, no run, and an empty warehouse, compiling once with the `false` body and once with the `true` body produced the full-refresh and steady-state SELECTs respectively. Keeping compilation data-free preserves dblect's static posture, since the analyzer never needs a populated warehouse to reach a world.

What we read from each compilation is the model's compiled SELECT, which is what the sqlglot-based detectors already analyze (`Node.analysis_sql`). The DML wrapper dbt adds around the SELECT (a CREATE-AS for the full build, a MERGE or DELETE+INSERT for the incremental apply) is not in the compiled SELECT and does not need to be here: its main analytic consequence, whether a merge-with-key dedups, is already carried by the enforcement facts the config discoverer derives from the materialization ([`config-and-flag-worlds.md`](./config-and-flag-worlds.md), the `unique_key` x `incremental_strategy` worked example). So the per-world SELECT is the right input, and the DML semantics stay with the property that understands them.

## The world-compiler

The execution substrate in `run.py` already does most of the setup: it copies the project to a temp directory, writes any seed and source fixtures, generates a `profiles.yml` pointing at an ephemeral DuckDB, and invokes dbt. The world-compiler reuses that setup and adds a small amount on top:

- Drop the `is_incremental()` override macro into the copied project's macro path.
- Run `dbt compile` twice against an ephemeral DuckDB connection, once with the override's `false` body and once with the `true` body. No seed, no run, no data, and no connection to the project's real warehouse.
- Read each world's compiled SQL through the existing `Manifest` reader: each compile writes a `target/manifest.json`, and `Node.compiled_code` already carries the per-world SELECT.

A world is therefore just a `Manifest` produced by the reader the project already uses, so no new artifact format or graph abstraction is introduced.

A compile that fails for a model in one world, or a model the override cannot reach (below), is recorded as opaque for that model in that world and degrades to the world we do have. It never aborts the run or silently drops a model, matching the analyzer's degrade-not-lie posture.

Reusing real dbt keeps fidelity high: the SQL we analyze is the SQL dbt produces, so we inherit its resolution of refs, sources, macros, and adapter dispatch. In-process compilation is a possible later optimization if invocation cost bites, the same trade `run.py` already names.

**Where the override does not reach.** The data-free path rests on the bare `{{ is_incremental() }}` call resolving to our macro. Two shapes fall outside it, and both degrade rather than mislead:

- An explicit `{{ dbt.is_incremental() }}` namespaced call, or a project that already defines its own `is_incremental`, is not shadowed by our injection. The steady-state world for such a model degrades to opaque, and its full-refresh world still compiles.
- A branch that introspects the existing relation at compile (`dbt_utils.star(this)`, `on_schema_change` handling) needs the relation to exist with its schema, which the data-free compile does not provide. These degrade to opaque, or can fall back to a build-based compile (build once so the relation exists, then compile) if that coverage is later wanted. The build-based fallback is the first probe's path, kept available for the schema-introspecting minority.

## Relationship to var support

This axis shares a substrate with the var-inference layer, and the constant override is chosen so the two stay cleanly separated.

The override body contains no `var()` or `env_var()`, so it adds nothing to the project's var namespace: there is no reserved var to collide with a user's vars, and nothing for var discovery to mistake for a project var. Var discovery reads the project's source through the [Jinja front end](https://github.com/dvryaboy/dblect/pull/104), never the world-compiled copy, so the injected macro is invisible to it regardless. The incremental axis therefore leaves var discovery and inference untouched.

Looking ahead, the world-compiler is the same component the control-flow flag evaluation ([#100](https://github.com/dvryaboy/dblect/issues/100)) will reuse. Both reduce to "compile the project under a forced world configuration, then harvest the manifest." For the incremental axis the forced configuration is the constant `is_incremental()` override; for a control-flow flag it is a var set to that world's value, passed to the same compile. A combined world applies both and harvests one manifest, and both feed the one `enumerate_worlds`. The incremental axis stays a single global run-mode axis (two worlds), the flag worlds compose as further axes, and cone scoping ([#99](https://github.com/dvryaboy/dblect/issues/99)) bounds the product. Building one world-compiler here that takes a world configuration, rather than an incremental-only mechanism, is what lets the flag layer reuse it.

## Wiring into the checker

Each world is a `Manifest`, so the existing analysis runs over it unchanged. `check_incremental_worlds` runs both detector families per world and keeps each world's findings under its `WorldRef`:

- `run_check` ([`check/run.py`](../../src/dblect/check/run.py)) carries the declaration-level findings: contract resolution, domain-type contradictions, not-well-typed aggregations.
- `run_audit` ([`audit/walker.py`](../../src/dblect/audit/walker.py)) carries the SQL-structural findings: join fan-out, window order, the nullability hazards, and the rest. The structure-adding case below is where this family earns its keep.

The two finding representations stay distinct (a declaration-level `CheckFinding` located by model and column, a structural `Finding` located by a span in one compiled statement); [#107](https://github.com/dvryaboy/dblect/issues/107) tracks whether to unify them. The cross-world diff treats them uniformly through a stable `FindingIdentity`. A finding present in steady-state but absent in full-refresh (or the reverse) is surfaced as a cross-world finding, with the world it holds in named; a finding present in both worlds is the same one the single-manifest analysis reports today, so the incremental axis strictly adds coverage.

The identity matters because these are control-flow worlds: the same issue renders with a different message and a different line span in each world, since the surrounding SQL differs. Keying the diff on a representation that ignores the volatile parts (kind, model, and column for a `CheckFinding`; kind, model, and the rendered offending snippet for a structural one) keeps one issue from being reported twice as world-varying. A whole-finding-equality diff would mistake the message drift for a difference.

A world whose compile did not succeed is omitted from the per-world findings and surfaced through `opaque_worlds`, so a one-world result is stated rather than allowed to masquerade as cross-world agreement. This mirrors the degrade-not-lie posture the rest of the analyzer takes.

## When the branch adds structure, not just a filter

A watermark filter is the common shape, but an `{% if is_incremental() %}` branch can add substantial SQL: a join to a state or lookup table, extra selected columns, a new upstream `ref`, a different grain. A worked example:

```sql
select s.*, st.last_seen
from {{ ref('source') }} s
{% if is_incremental() %}
  left join {{ ref('state_table') }} st on s.id = st.id
  where s.updated_at > (select max(updated_at) from {{ this }})
{% endif %}
```

The steady-state world differs from the full-refresh world in structure, not only in row count: a new join, a new column, a new dependency, and the nullability a `left join` injects.

This is the case the two-world compilation most wants to catch, and it handles it by construction. Each world is compiled in full and analyzed through the same detectors, which are SQL-in and indifferent to how large the difference is; there is no diff size that defeats the approach. The cross-world differencing then surfaces exactly what changed:

- a candidate key that holds in full-refresh but fans out under the steady-state join (a multiplicity finding in one world),
- a column that is `not null` in full-refresh but nullable under a steady-state `left join` (the INNER-versus-LEFT shape [`config-and-flag-worlds.md`](./config-and-flag-worlds.md) names),
- a column or upstream dependency present in only one world (a schema or lineage difference, reported per world).

Per-world lineage comes for free: each compilation's own `depends_on`, and the compiled SQL's own refs, describe that world's DAG, so a state table referenced only in the steady-state branch is an upstream in that world and absent in the other. The single-manifest audit is most blind precisely here, so this class is where the stream pays off most.

## Keeping the axis cheap, and the mixed-state refinement

The first delivery stands on its own and needs no cone work. The global two-world model avoids the `2^N` blow-up by construction (the axis is one project-wide run mode, so a project has two worlds whatever its model count), and its cost is a fixed doubling of the single-world path: two whole-project compilations and the detectors over two project views, no matter how much any branch diverges. The structure-adding case above costs no more than a watermark case, since it is still two worlds. The two optimizations below reduce the cross-world comparison and bound a later per-model refinement, and both reuse machinery the var-inference layer is building. Neither gates this stream, and neither is load-bearing for correctness, which always rests on analyzing both worlds in full. [#99](https://github.com/dvryaboy/dblect/issues/99) is a refinement layered on top once the global default is in, not a prerequisite for it.

**Skip the comparison only where the worlds are provably equivalent for a property.** When a branch is watermark-only, the two compiled SELECTs are structurally identical for the column set, the types, the candidate key, and the join-injected nullability, differing only in a row-filtering predicate. A uniqueness, type, or structural-nullability contract then agrees across the two worlds by construction, so the comparison is redundant and is reported as a collapse rather than run. This shortcut is taken only when the equivalence is evident from the two compiled SELECTs in hand, never as an assumption about what branches usually do. A branch that adds a join or a column is not equivalent, so it gets the full comparison and surfaces the findings above. Per-property scoping (the cone taken over the property's own provenance, as [`config-and-flag-worlds.md`](./config-and-flag-worlds.md) describes under property-dependence) is what recognizes the equivalence per property.

**Cone scoping bounds which models a contract sees.** A contract depends only on the incremental models in its lineage cone. Intersecting the cone with the incremental-model set ([#99](https://github.com/dvryaboy/dblect/issues/99)) means a contract never pays for incremental models it does not descend from.

The mixed-state worlds (a model backfilling while its siblings are steady-state, a selective `--full-refresh`) are the refinement these enable. Rather than a global `2^N` product, mixed worlds are introduced only inside a cone where the differing models are both reachable from the contract and relevant to its property. The global two-world default ships first and answers the dominant question; the cone machinery from [#99](https://github.com/dvryaboy/dblect/issues/99) layers the per-model and mixed worlds on top where they earn their cost. The two compose: start global, refine by cone.

## Scope and non-goals

- **`target` dispatch** is the sibling always-present axis (a small closed set of targets rather than two run modes). The world-compiler generalizes to it cleanly (compile once per target), and it is a natural follow-up, kept out of this stream to ship the incremental axis early.
- **Mixed and per-model incremental states** are deferred to the cone refinement above. The first delivery is the global two-world default.
- **DML-level semantics** (merge versus delete+insert versus insert) stay with the config-derived enforcement facts ([#39](https://github.com/dvryaboy/dblect/issues/39)); this stream analyzes the compiled SELECT per world.
- **No real-warehouse connection.** Compilation runs against an ephemeral DuckDB connection, never the project's Snowflake, BigQuery, or Databricks target. This is the deliberate stance: the stream stays data-free and connection-free until there is a concrete reason to require otherwise. Because `dbt compile` renders Jinja without executing model SQL, warehouse-specific SQL in a model body renders fine (it is text); a model whose adapter-dispatched macro lacks a DuckDB implementation degrades to opaque rather than failing the run. Compiling against the project's real adapter is a future option for higher absolute fidelity, taken when we have to rather than by default.

## Testing posture

Following the project's testing norms: pin contracts at the boundary, prefer property-based and exhaustive tests where they fit, avoid mocking and test theater. What is in place:

- A committed incremental dbt-project fixture (`tests/fixtures/incremental/`): a watermark model (`inc_watermark`) and a structure-adding model (`inc_stateful`) whose `is_incremental()` branch joins a `state` history log, so the steady-state world has an extra dependency, an extra column, and a `left join`. `state` carries its own surrogate key and several rows per `id`, which is what makes the steady-state join fan out.
- World-compiler tests (`tests/execution/test_incremental.py`) asserting that the two compiles, against an empty warehouse with no build, yield the watermark model's two worlds differing by exactly the `is_incremental()` branch, and that the structure-adding model's steady-state world carries the join, the extra column, and the new dependency its full-refresh world lacks. These pin the data-free mechanism the probes validated.
- The cross-world diff itself, pinned without dbt (`tests/check/test_cross_world.py`): a finding in one world only is surfaced, a finding in every world is world-invariant, and a finding whose message and line span drift between worlds is recognized as one issue rather than a false flip.
- The end-to-end cross-world finding (`tests/check/test_incremental_check.py`, dbt-gated): the steady-state-only join fans out a key the full-refresh build keeps unique, and the join-fan-out detector fires in steady-state alone. The cross-world diff surfaces exactly that one finding, carrying the world it holds in.

The dbt-gated tests resolve the CLI through the shared `dbt_cli` fixture, so they run under `uv run` and can be made to fail (rather than skip) in CI by setting `DBLECT_REQUIRE_DBT`. Degrade-not-lie tests for the shapes the override does not reach (an explicit `dbt.is_incremental()` call, a schema-introspecting branch) are a natural next addition; the opaque-world path they exercise is already in place.

## Resolved by the probes

The macro-override path settled the questions an earlier draft carried as open. Worlds are obtained data-free from `dbt compile` with an injected `is_incremental()` override, so the steady-state world needs no build, no warehouse data, and no state-table provisioning, and the full-refresh world is the override returning false rather than a `--full-refresh` run whose determinism we would have to confirm. Each world is read through the existing `Manifest` reader, so the harvest source is the per-world `manifest.json` `compiled_code`, and a world is a `Manifest` the existing pipeline already analyzes, which is the per-world view.

## Open questions

- **Scope of the override's reach.** The bare `{{ is_incremental() }}` call is shadowed by the injected macro (confirmed). The boundary is an explicit `{{ dbt.is_incremental() }}` call or a project that already defines its own `is_incremental`. How common these are in practice, and whether to detect a pre-existing project definition and adapt (rather than always inject and risk a collision), shapes how often the steady-state world degrades to opaque.
- **Schema-introspecting branches.** A branch that reads the existing relation's columns at compile (`dbt_utils.star(this)`, `on_schema_change`) cannot be reached data-free. Whether the build-based fallback is worth building for this minority, or whether degrading them to opaque is acceptable coverage for the first delivery.
- **Cross-adapter compile fidelity.** Compiling a non-DuckDB-target project against the ephemeral DuckDB connection renders model SQL faithfully (it is text) but resolves adapter-dispatched macros to their DuckDB variants. Since both worlds share the same substrate, this is common-mode and largely cancels in the cross-world difference the stream reports; the open piece is bounding where it does not (a dispatched macro that appears only in the steady-state branch) and deciding the threshold at which a real-adapter compile becomes worth requiring.

## References

- The world theory and the always-present-axis framing: [`config-and-flag-worlds.md`](./config-and-flag-worlds.md).
- The execution substrate this generalizes: [`src/dblect/execution/run.py`](../../src/dblect/execution/run.py).
- The world enumerator the axis feeds: [`src/dblect/check/worlds.py`](../../src/dblect/check/worlds.py).
- The config-derived facts that identify incremental models and carry enforcement: [#39](https://github.com/dvryaboy/dblect/issues/39).
- The cone scoping that refines the axis: [#99](https://github.com/dvryaboy/dblect/issues/99).
