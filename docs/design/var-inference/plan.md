# Var inference and flag scaffolding: implementation plan

Status: design (living document; rewritten into an architecture doc at merge)
Audience: engineers implementing dblect's flag discovery layer ([#98](https://github.com/dvryaboy/dblect/issues/98)) and reviewers of the work
Scope: v1 of var handling, `var()` and `env_var()` only

This is the big-picture plan for the var-inference engine and `dblect scaffold flags`. It sits above the original specification in [`var-inference-spec.md`](../var-inference-spec.md), which it refines rather than replaces: the spec defines the algorithm, the output format, and the inference rules; this plan organizes the build into work streams, records the design decisions taken since the spec was written, and links each work stream to its own design doc. The per-stream docs carry the detail.

The convention for this document set follows the project's design-doc workflow: these docs live in the implementation PR and are iterated on through design and implementation. When the work is ready to merge, they are rewritten into architecture docs that describe what was built and the key decisions behind it.

## Where this sits

The flag system has two halves. The **bridge** half is built and runs end to end: [`src/dblect/check/flags.py`](../../../src/dblect/check/flags.py) lowers a hand-authored `DomainFlag` to the per-world `CompileFact`s the enumerator consumes, and `check_worlds` produces cross-world findings from it. That half deliberately runs on hand-declared flags today, with responsiveness named directly on each flag, because the **discovery** half does not exist yet.

This work is the discovery half. It walks a dbt project, finds every `var()` and `env_var()`, infers enough about each to enumerate worlds (type, domain, default, branch points), classifies each by how it is used, and writes a draft flag file plus a diagnostic report that the user completes. It supplies the two inputs that let the bridge drop its hand-declarations: the **domain** to enumerate worlds over, and the **per-model var usage** that grounds model-responsiveness. It is also the prerequisite for wiring `check_worlds` into the `dblect check` CLI, since the CLI needs a flag-declaration surface to read.

The broader theory and the world-evaluation strategy live in [`config-and-flag-worlds.md`](../config-and-flag-worlds.md); the user-facing surface lives in [`flags_and_configs_as_types.md`](../flags_and_configs_as_types.md). This plan is the engineering view of the discovery layer those two docs depend on.

## What this feeds: #99 and #100

Two follow-on issues consume this layer's outputs, so the outputs are designed for them from the start.

- [#99 (cone-based per-contract world scoping)](https://github.com/dvryaboy/dblect/issues/99) consumes the **control-flow subset** of vars (the classification output) and the **per-model var usage** map. It intersects each contract's lineage cone with the responsive scopes of each control-flow flag to bound the world space by the largest interaction cluster rather than the global flag count.
- [#100 (control-flow world evaluation)](https://github.com/dvryaboy/dblect/issues/100) consumes the same classification and per-model usage, plus #99's clusters, to produce both compiled SQL forms of a branch flag.

The load-bearing consequence, beyond the original spec, is that **classification must land before naive enumeration**. Discovering every var and surfacing it as a world axis does not scale: a real project has hundreds of vars, most of which are not world axes. Each var is classified by the union over its usage sites into one of three classes:

- **Control-flow**: used in `{% if %}`, `{% for %}`, `is_incremental()`, or an equality / in-set / numeric test that steers a branch. This is the only class surfaced as a world axis, because it can change the compiled SQL. A var used in any control-flow context is control-flow, even if it is also substituted as a value elsewhere.
- **Value-substitution**: only ever substituted as a literal, never steering a branch. Collapses to one world, the value the manifest already compiled. Recorded and typed, not surfaced as an axis for now (the fact-level enumerator already handles value-substitution worlds, so enumerating these later is cheap).
- **Computed**: value not statically resolvable (a macro that queries the warehouse, an exotic Jinja pattern). One world, the resolved value, degrade-not-lie.

Coverage reports which axes collapsed and why, so a one-world var is a stated number rather than a silent assumption.

## Work streams

The build divides into work streams, each with its own design doc. The order below is the dependency order, not a fixed schedule.

1. **[Jinja front end](./jinja-frontend.md).** The parsing environment and the typed AST walker that turns a node's source Jinja into `VarUsage` records for direct `var()` / `env_var()` references, with source locations and syntactic context. The custom-tag and snapshot concerns are resolved here, with measured evidence.
2. **[Discovery inputs](./discovery-inputs.md).** The two external surfaces the analysis reads before it walks: a macro registry built from the manifest (the bodies macro-following expands) and the project configuration (`dbt_project.yml` declared vars and defaults, `profiles.yml` target overrides).
3. **[Macro following](./macro-following.md).** The expansion engine that follows `var()` calls reached through macros: registry lookup, depth-limited recursion, cycle detection, lexical parameter substitution, symbolic evaluation of literal-argument conditionals, adapter dispatch, and the opacity rules for runtime-dependent and higher-order macros.
4. **[Inference and classification](./inference-and-classification.md).** The lattice that folds a var's usages into a type and a domain, the classifier that assigns each var its control-flow / value-substitution / computed class, and the per-model var-usage map. This is the stream whose outputs #99 and #100 consume.
5. **[Scaffold output and CLI](./scaffold-and-cli.md).** The generated flag file, the diagnostic report, the re-run merge semantics that preserve user edits, the `dblect scaffold flags` command, and the reconciliation between the authored flag surface the spec describes and the bridge flag the enumerator consumes.

The first two streams are pure inputs and the most self-contained; the front end carries the resolved parsing risk. The inference and scaffold streams are where the user-visible behavior lands. Macro following is separable from direct-usage discovery and can follow it.

## Resolved risks

**Jinja parsing is not the risk.** A probe of `jinja2.Environment().parse()` against representative dbt templates and against the macro bodies in the jaffle fixture established that the parser yields a clean, structured AST in which every `UsageContext` the spec needs maps directly to a node shape, with literals resolved inline (including the inline `var(name, default)` the spec noted as a boundary). The control-flow versus value-substitution distinction, which #99 and #100 hinge on, is recoverable from the AST alone.

The custom-tag concern resolved cleanly. The only parse failures across the fixture's macro bodies were a small, known set of tag names: two are standard Jinja extensions dbt enables (`do`, and the loop controls `continue` / `break`), and the rest are dbt block tags (`materialization`, `snapshot`, `test`, and the same-shaped `docs`). Enabling the two standard extensions and adding one generic block-tag extension that parses *into* the body (so a `var()` inside a snapshot, including one nested in an `{% if %}`, keeps its syntactic context) brought the fixture's macro bodies to a clean parse. Anything genuinely unknown still degrades to an opaque diagnostic rather than a crash. The front-end doc carries the detail and the asserting test that keeps a future dbt tag addition from silently degrading. Snapshots are therefore in scope, read through the same body-parsing path, with the manifest supplying snapshot config (validity columns) separately.

## Open decisions

These are genuinely open and are expected to settle during the PR's design iteration.

1. **The authored flag surface versus the bridge flag.** The user-facing docs describe a `DomainFlag` with `dbt_var` / `type` / `domain` / `default` / `affects = RefinementEffect(...)`, and the spec's scaffold output emits classes of that shape. The flag the enumerator consumes today ([`flags.py`](../../../src/dblect/check/flags.py)) is the bridge form (`name`, `affects: Mapping[value, type]`, `scopes`), and `RefinementEffect` does not exist in code yet. The scaffold stream owns reconciling these: whether #98 introduces the authored surface and its effect types, or scaffolds the bridge form directly and tracks `RefinementEffect` separately. The [scaffold doc](./scaffold-and-cli.md) carries the options.
2. **First-PR scope.** Whether the initial PR lands direct-usage discovery, classification, and the scaffold command (deferring macro following and polish to follow-ups, matching the spec's staged delivery), or attempts the fuller engine in one pass.
3. **The spec's standing open questions.** Vars in seeds and sources, closed-world versus open-world default for inferred domains, declared-but-unused vars, per-package vars, scaffold versioning. These are listed in [`var-inference-spec.md`](../var-inference-spec.md) and resolved per stream as the streams that own them are designed.

## Testing posture

Following the project's testing norms: pin contracts at the boundaries, prefer property-based and exhaustive tests where they fit, avoid mocking and test theater. Each stream names its own testable contracts in its doc. Two cross-cutting commitments:

- The front end carries a test that every macro body in the fixture set parses, so a new dbt tag surfaces as a failing test rather than silent opaque-ing.
- Fixtures grow as small dbt projects that exercise each inference rule and each classification class, with the var-bearing projects the current fixtures lack. The aggregation and scaffold streams snapshot their output against these.

## References

- The original specification: [`var-inference-spec.md`](../var-inference-spec.md).
- The world theory and evaluation strategy this discovery layer feeds: [`config-and-flag-worlds.md`](../config-and-flag-worlds.md).
- The user-facing flag surface: [`flags_and_configs_as_types.md`](../flags_and_configs_as_types.md).
- The facts substrate the bridge lowers to: [`lineage-facts.md`](../lineage-facts.md).
- Per-stream design docs: [jinja-frontend](./jinja-frontend.md), [discovery-inputs](./discovery-inputs.md), [macro-following](./macro-following.md), [inference-and-classification](./inference-and-classification.md), [scaffold-and-cli](./scaffold-and-cli.md).
</content>
</invoke>
