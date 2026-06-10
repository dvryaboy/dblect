# Substrate readiness for domain-type propagation

*Status: design notes, engineering assessment. This records what the lineage/facts substrate already provides for a domain-type property (the [declaration DSL](declaration-dsl.md) surface and the [algebra](domain-type-algebra.md) it rests on), what genuinely needs building, and how joins reuse machinery that already exists. The findings come from a read of the substrate as it stands; file references are pointers to the code that backs each claim and should be re-checked as the substrate evolves.*

## The precondition holds

The capability the whole approach depends on, propagating a declared meaning from one model to an undeclared model downstream, is cross-model column-level lineage, and it is solid. `ColumnRef(source, column)` is a frozen, hashable handle for a column across the DAG (`lineage/graph.py`), the builder stamps it onto each sqlglot AST node as it qualifies a model's SQL (`lineage/builder.py`), and the propagator recurses through the stamped reference to stitch chains across model boundaries (`lineage/property.py`). The per-column expression is retained, not flattened to a name (`lineage/graph.py`, `expressions` map), so an operator walk can see `a + b`, `a / b`, and `sum(x)` with their operands.

A new property is a clean extension: a `Lattice` plus an optional `Semiring`, a set of fact discoverers, and per-operator and per-aggregate transfer rules, assembled through `column_property()` or `relation_property()` (`lineage/facts/property.py`). Nullability and uniqueness are worked examples to follow.

## What is already in place for the domain-type checks

Several ingredients we expected to build turn out to exist:

- **The aggregate coherence guard.** `CoherenceGuard` on an aggregate rule names a functional-dependency property and clears the aggregate to lattice top when that dependency does not hold at the scope (`lineage/facts/property.py`, applied in `lineage/property.py`). The "a sum is unsound unless a dependency discharges it" mechanism is wired as a hook with nothing feeding it yet.
- **The trust model.** Reconciliation offers both `reconcile_by_meet` (declared and inferred both hold, as uniqueness uses for keys) and tighten-or-taint (an inferred value must be consistent with the declared one or it taints provisional, as nullability uses), in `lineage/property.py`. This is the vouched-then-verified split the DSL draws for `Field` and for functional dependencies.
- **Constant binding from filters.** `predicate_flow` parses `currency = 'USD'` into atoms that ride lineage and activate conditional facts (`lineage/predicate.py`, `lineage/properties/predicate_flow.py`). This is the filter discharge and the pinned-tag propagation.
- **Uniqueness facts**, multi-column and cross-model, with join-preservation reasoning (`lineage/properties/uniqueness.py`).
- **Join key extraction** and **outer-join nullability taint** (`lineage/sql/_sqlglot.py`, `lineage/properties/nullability.py`).
- **Grounding and conflict detection** that folds facts through the lattice at each scope and fails the build on contradiction (`lineage/facts/grounding.py`).

## The two builds that are genuinely needed

**The multi-column companion binding.** Column-scoped properties today carry a single column's value; tuple awareness lives only at relation scope (uniqueness keys). A `Money` value spans `amount` and `currency`, so the domain-type value on the `amount` column must carry its tag bindings as references to peer columns (the `currency` column, or a literal when the currency is pinned). The framework permits this, because the property value type is generic, so the work is writing a lattice and transfer rules over a structured value rather than adding substrate plumbing. A useful property falls out for free: when `amount` flows to a model where the currency column was projected away, that peer reference no longer resolves, the binding degrades to top, and the coherence guard blocks a downstream sum until a dependency discharges it. That is the naked-amount taint, obtained from lineage resolution rather than special-cased.

**A first-class functional-dependency property.** The coherence guard reads an `fd`; nothing currently produces one. A functional-dependency property would ground from a `@contract` declaration returning `determines(...)`, infer from a key lookup (a key on the joined-in side of a dimension entails that its other columns are determined by that key), and propagate through joins. Uniqueness is both the template and a partial source, since a key is a determination of every other column.

The substrate items the audit marked partial (aggregation-argument tracking, output-column-to-input attribution) are reachable through the expression walk a column-scoped property already performs, so they are not blockers for the first working version.

## How joins reuse what exists

A join pairs rows rather than adding magnitudes, so its obligations are about keys, grain, dependencies, and nulls, and each maps onto machinery already present:

| Join concern | Reuses | New work |
|---|---|---|
| `ON`-clause key type compatibility | join key extraction (`_sqlglot`) plus the comparison rule on the domain-type property | a transfer rule for equality in join context |
| fan-out double counting | uniqueness join-preservation (`uniqueness.py`) plus column lineage to a magnitude's origin key | read uniqueness at the sum via a cross-property dependency, and flag a sum whose origin key was not preserved |
| dependency flow and creation through joins | uniqueness-style join transfer | the functional-dependency property (the second build above) |
| outer-join NULL tags | outer-join nullability taint (`nullability.py`) | declare the domain-type property dependent on nullability and treat a possibly-null tag as top |

Fan-out is the highest-value of these and rests almost entirely on uniqueness, which already reasons about whether a side's key survives a join. Outer-join null tags are nearly free through a cross-property dependency. Key-type compatibility is a small transfer rule. Dependency-through-join rides on the functional-dependency build.

## Suggested order

The critical path to the currency story working end to end is the companion binding, then the functional-dependency property feeding the existing coherence guard. With those, the aggregation findings and their discharges work. Joins then come in order of cost: outer-join null tags (reuse), key-type compatibility (small), fan-out double counting (uniqueness-powered, high value), and dependency-through-join (rides on the functional-dependency property). None of this calls for reworking the substrate; it is new properties and transfer rules over an engine that already carries the load.
