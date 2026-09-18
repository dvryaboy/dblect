# Var inference: scaffold output and CLI

Status: design
Audience: engineers implementing the generated flag file, the diagnostic report, and the `dblect scaffold flags` command
Part of: [the var-inference plan](./plan.md)

This stream renders the `DiscoveredVar` records from [inference and classification](./inference-and-classification.md) into the two artifacts the user reviews, a draft flag file and a diagnostic report, and wires the `dblect scaffold flags` command that produces them. It owns the one design decision the rest of the plan defers to it: the relationship between the flag surface the user authors and the flag the enumerator consumes.

## The flag-surface reconciliation

The user-facing docs ([`flags_and_configs_as_types.md`](../flags_and_configs_as_types.md)) and the spec's output templates describe a `DomainFlag` of this shape:

```python
class IncludeTaxInRevenue(DomainFlag):
    """TODO: describe what this flag controls."""
    dbt_var = "include_tax_in_revenue"
    type = bool
    domain = [True, False]
    default = False
    affects = RefinementEffect(target=Revenue.contains_tax, value_when_true=True, value_when_false=False)
```

The flag the enumerator consumes today ([`src/dblect/check/flags.py`](../../../src/dblect/check/flags.py)) is the bridge form:

```python
@dataclass(frozen=True, slots=True)
class DomainFlag:
    name: str
    affects: Mapping[Hashable, type[DomainType]]   # value -> fully-refined type
    scopes: tuple[ColumnRef, ...]                   # declared responsiveness
```

These differ in three ways: the authored surface keys `affects` to a `RefinementEffect` over a type axis while the bridge keys it to a fully-refined type per value; the authored surface carries `dbt_var` / `type` / `domain` / `default` that the bridge does not; and `RefinementEffect` does not exist in code. The two are not in conflict so much as at different levels: the authored surface is what a person writes, the bridge form is what the enumerator reads, and something must lower one to the other (the same lowering the config-and-flag-worlds doc calls "lowering `affects` to a fact").

Three ways to resolve it, to settle during the PR:

- **Scaffold the authored surface, add the lowering.** Introduce the authored `DomainFlag` (with `dbt_var` / `type` / `domain` / `default` / `affects`) and `RefinementEffect` in the types layer, scaffold that, and add a lowering from the authored flag to the bridge form the enumerator already consumes. This matches every user-facing doc and is the most faithful to the spec's templates. Its cost is introducing `RefinementEffect` and its lowering, which is arguably its own piece of work.
- **Scaffold the bridge form directly.** Generate the existing bridge `DomainFlag` (`name` / `affects: Mapping` / `scopes`), filling `name`, `domain` (as the `affects` keys), and leaving the per-value type and `scopes` as the user's `TODO`. This is the smallest change and keeps one flag class, at the cost of diverging from the authored surface the docs promise and asking the user to write the lower-level form.
- **Scaffold the authored surface, defer the lowering.** Generate the authored `DomainFlag` shape as the scaffold target and track `RefinementEffect` plus its lowering as a separate issue, so #98 produces the file the user edits and the enumerator wiring lands with the lowering. This keeps #98 focused on discovery and matches the docs, at the cost of a scaffold whose output the enumerator cannot yet read end to end.

The recommendation leans toward scaffolding the authored surface (the docs are written around it and the `affects` clause is the whole point of the user's involvement), with the lowering's scheduling, in #98 or a follow-up, decided by first-PR scope. This decision is recorded as open in [the plan](./plan.md) and is the first thing to lock.

## The generated flag file

Default path `dblect/flags/discovered.py`, overridable. One class per `DiscoveredVar`, sorted by name, in the resolved flag shape. The class carries the inferred type, the inferred domain (with the tentative note where the domain came from observation), the default from project config, source-location comments showing where the var was used (so the user can verify discovery and find missed usages), and a `TODO` on `affects` and the docstring. The spec's templates show the boolean, enum, open-domain, and inference-failed variants; the renderer produces each from the var's inference quality.

Only the **control-flow subset** is surfaced as flag classes with world axes; value-substitution and computed vars are recorded in the report (and optionally as commented or quality-marked entries) so the user sees them without their being enumerated. This keeps the file to the vars that are actually world axes, the scaling decision from [classification](./inference-and-classification.md).

## The diagnostic report

Default path `dblect/flags/discovery_report.md`. A summary (counts of discovered vars and env_vars, fully / partially / failed inference), a per-variable section with inferred properties and status, an inference-failed section with reasons and recommendations, and an unfollowed-usages section naming the macros and patterns that could not be followed and the vars they affect. The report is where coverage lands: which vars were surfaced as axes and which collapsed to one world and why. The spec's report layout is the template.

## Re-run merge semantics

`dblect scaffold flags` is run repeatedly as the project evolves, and must not clobber the `affects` clauses and docstrings the user has written. The merge rule, from the spec's open question now treated as a requirement:

- A class with a non-default `affects` (the user has filled it in) is user-owned and preserved unchanged.
- Inference-derived fields (type, domain, source-location comments) on classes still at their `TODO` default are updated to the latest inference.
- When a preserved class's inferred type changes between runs (the SQL changed), the class is left as the user wrote it and a diagnostic notes the difference, so the user decides whether to accept the new inference.
- New vars produce new draft classes; vars that disappeared are noted in the report rather than silently removed.

This makes re-running safe and the recommended way to keep declarations in sync. A format-version marker on the generated file lets a future tool version detect and migrate older output.

## The CLI command

A `scaffold` command group with a `flags` subcommand under the existing Typer app in [`cli/__init__.py`](../../../src/dblect/cli/__init__.py), alongside `init`, `audit`, and `check`. It resolves the manifest the same way those commands do (the shared `_resolve_manifest_path` and `_load_manifest` helpers, falling back to `dbt compile`), reads the [project config](./discovery-inputs.md), runs discovery and inference, and writes the two artifacts, never overwriting a user-owned class. Its options mirror the existing commands (`project_dir`, `--manifest`, `--dbt-executable`), plus output-path overrides for the flag file and report.

## Testing

- Snapshot tests of the generated file and report against the var-bearing fixtures from [discovery inputs](./discovery-inputs.md), one snapshot per inference-quality variant (boolean, enum, open-domain, inference-failed).
- A re-run test: scaffold, hand-edit a class's `affects` to a non-default, re-scaffold, and assert the edited class is preserved while a `TODO`-default class is updated.
- A generated-file validity test: the rendered Python imports and the classes construct (a generated file that does not import is a broken scaffold).
- A CLI test exercising `dblect scaffold flags` end to end against a fixture project, asserting both artifacts are written and exit status.

The snapshots pin the user-visible output; the re-run and validity tests pin the merge contract and that the output is usable, which are the properties that matter to the user rather than the renderer's internals.

## Open questions

- **The flag-surface reconciliation** above, the gating decision for this stream.
- **Declared-but-unused vars.** Whether a var declared in project config with no usage produces a class (it may be used in future) or is noted only in the report (noise reduction). The spec leaves this open.
- **Output location and packaging.** Whether `dblect/flags/` is the right home given the existing `dblect/` declaration tree the `init` command scaffolds, and how the discovered flags are imported into a `check` run.

## References

- [The var-inference plan](./plan.md) and [the original spec](../var-inference-spec.md), "Output format" and "Open questions".
- The authored flag surface: [`flags_and_configs_as_types.md`](../flags_and_configs_as_types.md). The bridge and the lowering: [`flags.py`](../../../src/dblect/check/flags.py), [`config-and-flag-worlds.md`](../config-and-flag-worlds.md).
- The records this renders: [inference and classification](./inference-and-classification.md).
</content>
