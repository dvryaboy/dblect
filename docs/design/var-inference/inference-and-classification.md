# Var inference: inference and classification

Status: design
Audience: engineers implementing type inference, domain inference, and var classification
Part of: [the var-inference plan](./plan.md)

This stream folds the `VarUsage` records the [front end](./jinja-frontend.md) and [macro-following](./macro-following.md) produce into one `DiscoveredVar` per variable: its type, its domain, its class, and the per-model usage map. Its outputs are what #99 and #100 consume, so the classification and the per-model map are designed for them, not only for the scaffold.

## Aggregation

After all nodes are walked, group `VarUsage` records by variable name. For each variable, run type inference, domain inference, and classification across its usages, cross-reference the [project config](./discovery-inputs.md) defaults and target values, and produce a `DiscoveredVar` as specified in [`var-inference-spec.md`](../var-inference-spec.md): name, kind, inferred type, inferred domain, default, target values, the usage list, an inference-quality marker, and the unfollowed usages.

## Type inference

Type inference reads each usage's `UsageContext` into a type assertion and combines assertions across usages via the type lattice. The rules are the spec's table: a truthy test asserts `bool`, an equality or in-set asserts the type of its operands, a numeric inequality or arithmetic asserts numeric, a SQL-literal position asserts a type from its position (numeric, quoted-string, identifier). Compatible assertions narrow; incompatible assertions are a conflict.

A conflict (one usage asserts `str`, another `int`) is reported in the diagnostic output, the most permissive type wins for scaffolding, and a comment notes the conflict so the user adjudicates. The project-config default is an additional signal: a boolean default with no contrary usage infers `bool`, a string default infers `str`, a numeric default infers `int` or `float`.

## Domain inference

Domain inference identifies the value set a variable can take, defaulting to open-world unless evidence supports a finite domain. The spec's rules:

- **Finite** when all usages are equality or in-set contexts (the domain is the union of those literals), or when the variable is boolean (a two-element domain). The default and target values must be members, or the inference is marked inconsistent.
- **Partial** when some usages support a finite domain and some do not. The domain is the union of observed literals, noted as possibly incomplete.
- **Branch points** when numeric comparisons are observed. The branch points partition the number line into intervals, and world enumeration covers each interval rather than each value (`var('threshold') > 100` yields the two worlds `threshold <= 100` and `threshold > 100`).

Inferred domains are always reported as tentative, review for completeness. Static analysis sees what the SQL uses, not what a CI config or environment might supply, so the user has the final say on the closed-world reading. Default and target values are added to the domain as observed members without changing the open-versus-finite classification.

## Classification: the gate before enumeration

This is the requirement [#98](https://github.com/dvryaboy/dblect/issues/98) adds beyond the original spec, and the one #99 and #100 hinge on. Discovering every var and surfacing it as a world axis does not scale: a real project has hundreds of vars, most of which cannot change the compiled SQL. Each variable is classified by the union over its usage sites into one of three classes:

- **Control-flow.** Used in `{% if %}`, `{% for %}`, `is_incremental()`, or an equality / in-set / numeric test that steers a branch. Recoverable from the AST shape alone (the [front end](./jinja-frontend.md) records whether a usage was reached under a branch-steering position). This is the only class surfaced as a world axis, because it can change the SQL structure. A var used in any control-flow context is control-flow, even when it is also substituted as a value elsewhere; the union takes the stronger class.
- **Value-substitution.** Only ever substituted as a literal, never steering a branch. The SQL structure is invariant across its values, so it collapses to one world, the value the manifest already compiled. It is recorded and typed but not surfaced as an axis for now. The fact-level enumerator already handles value-substitution worlds, so enumerating these later is cheap; the deferral loses nothing.
- **Computed.** Value not statically resolvable: reached only through an opaque macro, or carrying an open domain with no enumerable values. One world, the resolved value, degrade-not-lie.

The class is a property of the variable, computed from the join of its usage contexts. A variable with a single control-flow usage among many value-substitution usages is control-flow. A variable all of whose usages are opaque is computed. The classifier is pure and total over the usage set, which makes it exhaustively testable.

### Why the union, and why this is sound

Surfacing only the control-flow subset as world axes is the first variability abstraction in the design's terms (see [`config-and-flag-worlds.md`](../config-and-flag-worlds.md), "Taming the world space"). It is sound because a value-substitution var cannot change the SQL structure, so analyzing its single compiled world is correct for that var; and it degrades honestly because the collapse is recorded in coverage rather than assumed. Taking the union toward the stronger class is the conservative direction: it never wrongly demotes a control-flow var to a collapsed world, which would hide a real structural difference.

## The per-model var-usage map

The second output #99 and #100 consume is which models read which vars. The model-responsiveness rule the bridge uses grounds a flag only in models that actually read its var, and #99's cone scoping intersects this with the lineage cone. The map falls out of the walk directly: each `VarUsage` carries the node it was found in (and the macro trail it came through), so grouping usages by node yields the per-model map. It is emitted alongside the `DiscoveredVar` records, scoped to the control-flow subset for #99's consumption (the spec notes per-contract narrowing runs over the control-flow subset, not all discovered vars).

## Coverage

Classification feeds coverage reporting: the analysis records which vars were surfaced as axes, which collapsed to one world and why (value-substitution, computed, open domain), so a one-world var is a stated number rather than a silent assumption. This is the "no silent caps" rule applied to the world dimension, raised in [`config-and-flag-worlds.md`](../config-and-flag-worlds.md), "Coverage as a first-class output".

## Testing

- Property-based tests over synthetic usage sets: the type lattice fold is associative and commutative (order of usages does not change the inferred type), and the classifier's union toward the stronger class is order-independent.
- Per-rule tests for each type and domain rule, mirroring the front-end's per-context tests but at the aggregation boundary.
- Classification tests: a var with mixed control-flow and value-substitution usages classifies control-flow; an all-opaque var classifies computed; a pure-literal var classifies value-substitution.
- A branch-point test asserting the interval partition for numeric comparisons.
- A per-model-map test asserting a var used in two models maps to both, including one reached through a macro.

These pin the contracts (what a usage set infers and classifies to) rather than the fold's internals, so they survive a reorganization of the inference code.

## Open questions

- **Closed-world versus open-world default for inferred domains.** The spec marks inferred enum domains tentative. Whether a more explicit confirmation step is needed before treating an inferred domain as authoritative for enumeration.
- **Numeric branch-point representation.** The exact `Domain` shape for interval-partitioned numeric domains (the spec leaves `Domain` as a list of intervals); pinning it here so the [scaffold](./scaffold-and-cli.md) and the enumerator agree.

## References

- [The var-inference plan](./plan.md) and [the original spec](../var-inference-spec.md), "Type inference rules" and "Domain inference rules".
- The classification's consumers: [#99](https://github.com/dvryaboy/dblect/issues/99), [#100](https://github.com/dvryaboy/dblect/issues/100), and the world theory in [`config-and-flag-worlds.md`](../config-and-flag-worlds.md).
- The usage records this folds: [the Jinja front end](./jinja-frontend.md), [macro following](./macro-following.md).
</content>
