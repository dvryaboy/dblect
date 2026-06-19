# Incremental worlds: checking incremental models in both compilations

A dbt incremental model compiles two ways, a full-refresh form and a steady-state
form (the `{% if is_incremental() %}` branch), and dbt requires both to be valid. A
single manifest captures one, so a hazard in the unexercised branch is invisible to
the single-manifest analysis. This check compiles a project both ways, runs the
project's detectors over each, and reports a finding that holds in one world and
breaks in the other.

The code: [`execution/incremental.py`](../../src/dblect/execution/incremental.py) is
the world-compiler; [`check/incremental.py`](../../src/dblect/check/incremental.py) is
the per-world check and cross-world diff. This builds on the world theory in
[`../design/config-and-flag-worlds.md`](../design/config-and-flag-worlds.md).

## Why it is built this way

**Both worlds come from `dbt compile` alone, data-free.** The world-compiler shadows
`is_incremental()` with a constant-returning root-project macro and compiles once per
value. `ref()` and `{{ this }}` resolve at parse, so the steady-state SELECT compiles
with nothing built: no seed, no run, no warehouse connection. This keeps the analyzer
static and lets it reach the steady-state branch without provisioning state tables.

**Each world is compiled independently, not re-grounded from a shared build.** These
are control-flow worlds (the SQL itself differs), unlike the value-substitution worlds
the enumerator in `check/worlds.py` serves, where one build is re-grounded because the
SQL is identical. Different SQL means no shared build.

**The cross-world diff keys on a stable `FindingIdentity`, not whole-finding
equality.** Because the SQL differs between worlds, the same issue renders with a
different message and line span in each, which a naive equality diff would report
twice as world-varying. The identity ignores those volatile parts. The two finding
representations it spans (a declaration-level `CheckFinding`, a structural `Finding`)
are the subject of [#107](https://github.com/dvryaboy/dblect/issues/107).

**Both detector families run per world.** `run_check` carries the declaration-level
findings, `run_audit` the SQL-structural ones. The hazard this most wants to catch
lives in the structural family: a key the full-refresh build keeps unique fanned out
by a steady-state-only join, so the join-fan-out detector fires in steady-state alone.
The committed fixture under `tests/fixtures/incremental/` carries that shape.

**A world whose compile fails degrades to opaque rather than aborting.** The override
reaches the bare `{{ is_incremental() }}` call; an explicit `dbt.is_incremental()` or
a branch that introspects the relation's schema at compile is not reached, and that
world is reported with its error while the other still stands.

## Planned refinements

Forward-looking work, tracked in [#108](https://github.com/dvryaboy/dblect/issues/108)
and [#99](https://github.com/dvryaboy/dblect/issues/99) (cone scoping): mixed-state and
per-model worlds scoped inside a contract's lineage cone, skipping the comparison where
a watermark-only branch makes the worlds provably equivalent for a property, `target`
dispatch as the sibling axis, and degrade-not-lie coverage for the override-unreachable
shapes.
