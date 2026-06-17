# Incremental worlds: checking incremental models in both compilations

Status: design
Audience: engineers building the world-compiler and wiring the incremental axis into the checker. It builds on the world theory in [`config-and-flag-worlds.md`](./config-and-flag-worlds.md) (how a configuration becomes facts and what it means to check across more than one world), the execution substrate in [`src/dblect/execution/run.py`](../../src/dblect/execution/run.py), and the world enumerator in [`src/dblect/check/worlds.py`](../../src/dblect/check/worlds.py).

A dbt incremental model compiles to two different SQL statements: a first-run / full-refresh form that builds over all rows, and a steady-state form whose `{% if is_incremental() %}` branch is present. The dbt docs require both to be valid, so both are reachable by construction, yet a single manifest captures exactly one. This stream compiles a project both ways and runs the existing detectors over each, so a contract that holds in one world and breaks in the other becomes a finding instead of a blind spot.

This is the first of the always-present control-flow axes named in [`config-and-flag-worlds.md`](./config-and-flag-worlds.md). It is deliberately scoped ahead of the rest of the var-inference layer because it applies to any project with an incremental model, asks the developer for nothing, and rests on machinery that already exists.

## Where this sits

[`config-and-flag-worlds.md`](./config-and-flag-worlds.md) names the gap precisely: the current static analyzer is sound and useful, and it analyzes one compilation of every model that branches on configuration. For an incremental model that compilation is whichever branch dbt last produced, so any hazard (or any clean bill) in the unexercised branch is invisible. That doc also names the remedy and its cost: `is_incremental()` "has exactly two states ... so even before a general enumerator exists, compiling those specific worlds (two for incremental ...) closes the highest-frequency control-flow gap at a fixed, small cost."

Two pieces this stream leans on are already built. The config discoverer ([#39](https://github.com/dvryaboy/dblect/issues/39)) reads `materialized` and `incremental_strategy` from `node.config` into a typed `ModelConfig`, which tells us which models are incremental. The world enumerator (`enumerate_worlds` in `check/worlds.py`) takes a `WorldRef → compile facts` mapping and produces cross-world findings; it is source-agnostic, so the always-present axis feeds it the same way a hand-declared flag does.

## The world model: one global run-mode axis

The axis is the project's **run mode**, with two values: full-refresh (every incremental model takes its relation-absent branch) and steady-state (every incremental model takes its `is_incremental()` branch). The project is compiled once under each, giving two project-level worlds.

This is a global axis, not a per-model one, and that choice is load-bearing. Treating each incremental model as an independent binary axis would enumerate `2^N` worlds for `N` incremental models, which is unaffordable on a real project. The global framing gives two worlds regardless of `N`. It is also the operationally faithful reading: a dbt invocation puts the whole project in one mode. A normal `dbt run` builds every incremental model in steady-state, and `dbt run --full-refresh` rebuilds every one from scratch. Running one model incremental while another full-refreshes in the same logical run is the exception (a selective `--full-refresh`, a newly added model backfilling, a dropped relation), not the common case. Those mixed states are real and worth checking eventually; they are handled as a refinement rather than as the default, described under [Keeping the axis cheap](#keeping-the-axis-cheap-and-the-mixed-state-refinement).

The axis is present for a project exactly when at least one model is incremental-materialized, read from the `ModelConfig` the config discoverer already produces. A project with no incremental models has one world and this stream is a no-op for it.

## Obtaining both worlds

Both worlds come from `dbt compile` alone, with no build and no warehouse data. The lever is that `is_incremental()` is a macro, and a root-project macro of the same name shadows dbt's built-in for the bare `{{ is_incremental() }}` call that incremental models use. The world-compiler injects an override that returns a dblect-controlled var:

```jinja
{% macro is_incremental() %}
    {{ return(var('dblect_force_incremental', false)) }}
{% endmacro %}
```

Compiling with that var false yields the full-refresh world, and with it true yields the steady-state world. Because the override returns the value directly, neither compile runs an introspective query or depends on a relation existing: `ref()` and `{{ this }}` resolve to relation names at parse, so the steady-state SELECT compiles even though nothing has been built. The override forces every incremental model the same way in one compile, which is exactly the global run-mode world the [world model](#the-world-model-one-global-run-mode-axis) describes.

Two probes established this. The first confirmed the underlying dbt behavior: compiling a model twice against a persistent DuckDB produced the relation-absent SELECT and then, once the relation existed, the same SELECT with its watermark branch (`where event_time > (select max(event_time) from <this>)`). The second confirmed the path the stream adopts: with the override macro in place and no seed, no run, and an empty warehouse, `dbt compile --vars '{dblect_force_incremental: false}'` and the same with `true` produced the full-refresh and steady-state SELECTs respectively. Keeping compilation data-free preserves dblect's static posture, since the analyzer never needs a populated warehouse to reach a world.

What we read from each compilation is the model's compiled SELECT, which is what the sqlglot-based detectors already analyze (`Node.analysis_sql`). The DML wrapper dbt adds around the SELECT (a CREATE-AS for the full build, a MERGE or DELETE+INSERT for the incremental apply) is not in the compiled SELECT and does not need to be here: its main analytic consequence, whether a merge-with-key dedups, is already carried by the enforcement facts the config discoverer derives from the materialization ([`config-and-flag-worlds.md`](./config-and-flag-worlds.md), the `unique_key` x `incremental_strategy` worked example). So the per-world SELECT is the right input, and the DML semantics stay with the property that understands them.

## The world-compiler

The execution substrate in `run.py` already does most of the setup: it copies the project to a temp directory, writes any seed and source fixtures, generates a `profiles.yml` pointing at an ephemeral DuckDB, and invokes dbt. The world-compiler reuses that setup and adds a small amount on top:

- Drop the `is_incremental()` override macro into the copied project's macro path.
- Run `dbt compile` twice against the ephemeral warehouse, once with `dblect_force_incremental` false and once true. No seed, no run, no data.
- Read each world's compiled SQL through the existing `Manifest` reader: each compile writes a `target/manifest.json`, and `Node.compiled_code` already carries the per-world SELECT.

A world is therefore just a `Manifest` produced by the reader the project already uses, so no new artifact format or graph abstraction is introduced.

A compile that fails for a model in one world, or a model the override cannot reach (below), is recorded as opaque for that model in that world and degrades to the world we do have. It never aborts the run or silently drops a model, matching the analyzer's degrade-not-lie posture.

Reusing real dbt keeps fidelity high: the SQL we analyze is the SQL dbt produces, so we inherit its resolution of refs, sources, macros, and adapter dispatch. In-process compilation is a possible later optimization if invocation cost bites, the same trade `run.py` already names.

**Where the override does not reach.** The data-free path rests on the bare `{{ is_incremental() }}` call resolving to our macro. Two shapes fall outside it, and both degrade rather than mislead:

- An explicit `{{ dbt.is_incremental() }}` namespaced call, or a project that already defines its own `is_incremental`, is not shadowed by our injection. The steady-state world for such a model degrades to opaque, and its full-refresh world still compiles.
- A branch that introspects the existing relation at compile (`dbt_utils.star(this)`, `on_schema_change` handling) needs the relation to exist with its schema, which the data-free compile does not provide. These degrade to opaque, or can fall back to a build-based compile (build once so the relation exists, then compile) if that coverage is later wanted. The build-based fallback is the first probe's path, kept available for the schema-introspecting minority.

## Wiring into the checker

Each world is a `Manifest`, so the existing analysis pipeline runs over it unchanged, reusing the whole single-world path rather than a parallel one. The findings from each world carry that world's `WorldRef`, and `enumerate_worlds` differences them: a finding present in steady-state but absent in full-refresh (or the reverse) is surfaced as a cross-world finding, with the world it holds in named. A finding present in both worlds is the same finding the single-manifest analyzer reports today, so the incremental axis strictly adds coverage rather than changing existing behavior.

Coverage reporting states, per contract, whether both incremental worlds were analyzed or only one was reachable, so a one-world result is a stated number rather than a silent assumption. This mirrors the coverage posture the flag layer already takes.

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
- **Non-DuckDB compilation.** The substrate compiles against DuckDB for fidelity and speed. Compilation of the SELECT is largely adapter-independent; where an adapter's dispatch changes the compiled SQL, that is the `target`/adapter concern, tracked with the dispatch axis rather than here.

## Testing posture

Following the project's testing norms: pin contracts at the boundary, prefer property-based and exhaustive tests where they fit, avoid mocking and test theater.

- A small committed incremental dbt-project fixture (an incremental model with a watermark branch over a seed), the cleaned-up form of the probe project, shared with downstream streams that need an incremental project.
- A structure-adding fixture whose `is_incremental()` branch joins a state table, so the steady-state world has an extra dependency, an extra column, and a `left join`. This is the case that exercises the cross-world findings the design turns on.
- A world-compiler test asserting that compiling with `dblect_force_incremental` false and true, against an empty warehouse with no build, yields the watermark model's two worlds differing by exactly the `is_incremental()` branch, and that the structure-adding model's steady-state world carries the join and column its full-refresh world lacks. These pin the data-free mechanism the second probe validated.
- Degrade-not-lie tests for the shapes the override does not reach: a model that calls `dbt.is_incremental()` explicitly, and a branch that introspects the relation schema, each yielding an opaque steady-state world while the full-refresh world still compiles, rather than a wrong answer or an aborted run.
- End-to-end cross-world findings: a candidate key that fans out under the steady-state join, and a column nullable only under the steady-state `left join`, each surfaced as a finding carrying the world it holds in. A contract holding in both worlds is reported once, unchanged from the single-manifest path.

## Resolved by the probes

The macro-override path settled the questions an earlier draft carried as open. Worlds are obtained data-free from `dbt compile` with an injected `is_incremental()` override, so the steady-state world needs no build, no warehouse data, and no state-table provisioning, and the full-refresh world is the override returning false rather than a `--full-refresh` run whose determinism we would have to confirm. Each world is read through the existing `Manifest` reader, so the harvest source is the per-world `manifest.json` `compiled_code`, and a world is a `Manifest` the existing pipeline already analyzes, which is the per-world view.

## Open questions

- **Scope of the override's reach.** The bare `{{ is_incremental() }}` call is shadowed by the injected macro (confirmed). The boundary is an explicit `{{ dbt.is_incremental() }}` call or a project that already defines its own `is_incremental`. How common these are in practice, and whether to detect a pre-existing project definition and adapt (rather than always inject and risk a collision), shapes how often the steady-state world degrades to opaque.
- **Schema-introspecting branches.** A branch that reads the existing relation's columns at compile (`dbt_utils.star(this)`, `on_schema_change`) cannot be reached data-free. Whether the build-based fallback is worth building for this minority, or whether degrading them to opaque is acceptable coverage for the first delivery.
- **Var-name collision.** `dblect_force_incremental` is a reserved var the override reads. Confirm it cannot clash with a project's own vars, or namespace it so it cannot.

## References

- The world theory and the always-present-axis framing: [`config-and-flag-worlds.md`](./config-and-flag-worlds.md).
- The execution substrate this generalizes: [`src/dblect/execution/run.py`](../../src/dblect/execution/run.py).
- The world enumerator the axis feeds: [`src/dblect/check/worlds.py`](../../src/dblect/check/worlds.py).
- The config-derived facts that identify incremental models and carry enforcement: [#39](https://github.com/dvryaboy/dblect/issues/39).
- The cone scoping that refines the axis: [#99](https://github.com/dvryaboy/dblect/issues/99).
