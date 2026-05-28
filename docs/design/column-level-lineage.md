# Column-level lineage and property propagation

## Motivation

Three downstream needs converge on a single substrate.

1. **Multi-source uniqueness propagation.** The current uniqueness layer bails on join scopes (`src/dblect/uniqueness/detector.py:51`). Per-column lineage closes the gap.
2. **Cross-model fanout impact.** A `join_fanout` finding on model M needs to project forward to any downstream column whose values flowed through M's fan-out side.
3. **Semantic-types tag propagation.** When the semantic-types layer lands, each declared type binds to `(model_uid, column)` and propagates forward through the same lineage edges.

Each of these is a *property* we want to track on columns and propagate through SQL operators. The honest substrate is one that handles all of them under the same algebra, rather than three parallel passes with three ad-hoc rule tables.

## Framework: K-relations at the column level

We build on the provenance-semiring framework (Green, Karvounarakis, Tannen 2007). A property is a commutative semiring `(K, +, ×, 0, 1)` together with operator-specific transfer functions that interpret SQL operators as semiring operations:

- `+` reconciles values at *confluence* points (`UNION ALL`, multiple branches feeding the same downstream column)
- `×` combines values at *cross* points (the implicit cross product underlying every `JOIN`)
- `0` is the absorbing element for `×` (annotation for "absent" or "bottom")
- `1` is the identity for `×` (annotation for "neutral" or "fully present")

Choosing different K's encodes different properties under the same algorithm.

- **Bag cardinality (fanout).** `K = N` with arithmetic `+` and `×`. `JOIN` multiplies tuple multiplicity, `UNION ALL` adds. This is exactly the bag semantics that captures `join_fanout`'s downstream propagation.
- **Where-provenance.** `K = P(ColumnRef)`, set union for both `+` and `×`. Records which source columns ultimately fed each output column.
- **Uniqueness as candidate keys.** `K = P(P(ColumnRef))` (sets of candidate key sets). Sketch: `+` intersects branch key sets (`UNION ALL` retains a key only if both branches carry it), `×` concatenates keys across sides (`JOIN` combines keys subject to join-condition coverage). The detailed rules live in the `Property[K]` definition; this encoding is what lets the multi-source bail go away.
- **Nullability.** `K = {non-null, nullable, unknown}`, a 3-element lattice (an idempotent semiring with lattice join as `+` and lattice meet as `×`). Sources seed from declared `not_null` tests.
- **Semantic types (future).** `K` is the user-domain lattice from the types layer. Per-operator and per-aggregate transfers come from the type's declared transfer rules.

Aggregates need the semimodule extension (Amsterdamer, Deutch, Tannen 2011). Each aggregate function (`SUM`, `MIN`, `MAX`, `COUNT`, `ARRAY_AGG`, ...) is a `K → K` transfer that depends on the aggregate's algebra over `K`. We declare these per-property, keyed on the sqlglot aggregate expression subclass.

The result: one propagation engine, one walk over the lineage graph. Adding a new property is adding a `Property[K]`, not a new pass.

## Marrying with sqlglot

sqlglot's parse tree gives us the relational-algebra structure; its `lineage` module gives us where-provenance per output column (which source columns and through which SQL expression each output column was built). We layer on:

1. A `Semiring[K]` protocol that any property's K implements.
2. A `Property[K]` bundle: name, semiring, per-operator transfer functions keyed on `exp.Expression` subclass, per-aggregate semimodule transfers keyed on `exp.AggFunc` subclass, and a source rule (how the property gets its initial value from dbt declarations or source-schema facts).
3. A `Propagator` that consumes a `Property[K]` and a `ColumnLineageGraph`, walks the graph in topological DAG order, and produces a `Mapping[ColumnRef, K]`.

Operator dispatch is on sqlglot's `Expression` subclass directly. The sqlglot type hierarchy already enumerates the operators we need, so there is no parallel enum to keep in sync; unrecognised subclasses fall through to a property-defined default (typically lattice top or semiring `0` per property's semantics).

## Data model

```python
from typing import Protocol, runtime_checkable, TypeVar, Generic, Callable, Mapping
from dataclasses import dataclass
from sqlglot import expressions as exp

K = TypeVar("K")


@runtime_checkable
class Semiring(Protocol[K]):
    zero: K
    one:  K
    def plus(self,  a: K, b: K) -> K: ...
    def times(self, a: K, b: K) -> K: ...


@dataclass(frozen=True, slots=True)
class ColumnRef:
    source: SourceRef                  # model_uid, source_uid, seed_uid, or in-scope CTE name
    column: str


@dataclass(frozen=True, slots=True)
class ColumnLineageGraph:
    """How-provenance plus an immediate-upstream relation.

    `edges` maps each ColumnRef to the columns its projection expression
    directly references (one step only; the propagator stitches longer
    chains by recursion). CTE intermediates, derived-table projections,
    and UNION ALL outputs all appear as first-class ColumnRefs with
    synthetic SourceKinds, so a Column's stamp always points at a single
    upstream graph node.

    `expressions` maps each ColumnRef to the sqlglot Expression that
    produced it (how-provenance), letting per-property transfer functions
    dispatch on operator type.
    """
    edges:       Mapping[ColumnRef, frozenset[ColumnRef]]
    expressions: Mapping[ColumnRef, exp.Expression]


@dataclass(frozen=True, slots=True)
class Property(Generic[K]):
    name:     str
    semiring: Semiring[K]
    # Per-operator transfer: how a sqlglot Expression combines its input
    # column annotations into its output annotation.
    operators:  Mapping[type[exp.Expression], Callable[[exp.Expression, tuple[K, ...]], K]]
    # Per-aggregate transfer (the Amsterdamer-Deutch-Tannen semimodule extension).
    aggregates: Mapping[type[exp.AggFunc], Callable[[exp.AggFunc, K], K]]
    # Initial K from source-level facts (dbt tests, manifest schema).
    source: Callable[[ColumnRef], K]


def propagate(
    graph: ColumnLineageGraph,
    prop:  Property[K],
) -> Mapping[ColumnRef, K]:
    """Topologically propagate prop's K-annotation through graph."""
    ...
```

## Operator-to-semiring mapping

The propagator dispatches on the sqlglot expression type and consults `Property.operators` (or `Property.aggregates` for aggregates). The intended action per operator:

| sqlglot operator              | Action on column annotations                                                 |
|-------------------------------|------------------------------------------------------------------------------|
| `exp.Column` (bare ref)       | Identity: output = input annotation                                          |
| `exp.Alias` (no-op wrapper)   | Identity                                                                     |
| Scalar function / cast        | Per-function rule; default folds inputs via the property's `×`               |
| `exp.AggFunc` subclass        | `prop.aggregates[type]` applied to the partition's input annotation          |
| `exp.Window`                  | Per-property; default is the aggregate transfer applied per-partition        |
| `exp.Join`                    | `×` at the tuple level; column annotations pass through per side             |
| `exp.Union` (UNION ALL)       | `+` of branch annotations                                                    |
| `exp.Union` (set-semantics)   | `+` followed by property-specific dedup handling                             |
| `exp.Where`                   | No-op on column annotations (filters tuples, leaves column-level facts)      |
| `exp.Limit`                   | No-op on column annotations                                                  |
| `exp.Subquery`                | Recursive `propagate` on the subquery; the result is treated as a source     |

The table is the shape of dispatch. Each `Property[K]` fills in the specific behaviour for the cases it cares about; missing entries fall through to the property-defined default.

## Cross-model composition

The audit builds a single `ColumnLineageGraph` per run by walking the manifest DAG in topological order. For each model:

1. Parse the compiled SQL and qualify it via sqlglot's optimiser passes, then build the scope tree.
2. Walk the scope tree top-down. The root SELECT's projections become `ColumnRef`s on the model. Each CTE projection becomes a `ColumnRef` on a synthetic `cte.<model_uid>.<scope_path>` source; each UNION ALL output gets a synthetic `union.<model_uid>.<scope_path>.<col>` node whose expression is `Union(arm0, arm1, ...)`, with each arm projection itself a separate `ColumnRef`. Each `exp.Column` in any projection is stamped with the single `ColumnRef` of its immediate upstream graph node.
3. Merge into the audit-wide graph.

Property propagation is then a separate pass per property: `propagate(graph, prop)` returns a `Mapping[ColumnRef, K]`. Properties are independent. Running uniqueness propagation does not depend on running fanout propagation; they share the graph, never the annotations.

## Failure modes

The contract is that annotations degrade gracefully when sqlglot cannot fully resolve a column.

- **sqlglot resolution gap.** When sqlglot returns no usable `Node` for an output column, the column's entry in `expressions` is absent. The propagator emits the property's "unknown" value (typically lattice top for lattices, semiring `0` for cardinality). Detectors consuming the annotation interpret an unknown value as "we don't know" and skip the column. The audit reports a per-model "lineage incomplete" note so reviewers see what was silenced.
- **Macros that escape dbt rendering.** Same handling: affected columns get the unknown annotation; we surface the gap rather than guess.
- **Dialect-specific constructs sqlglot misparses.** Same handling.
- **Column-name collisions across set-op branches.** sqlglot's qualification disambiguates these. Where qualification fails, the column gets the unknown annotation.

Annotations are never load-bearing for correctness: false negatives on lineage-dependent detectors are acceptable; wrong annotations are not. This is the same contract the uniqueness layer holds today.

## Testing

- **Semiring laws.** PBT on every `Semiring` instance for associativity, commutativity, identities, distributivity, and absorption. Catches structural bugs in property definitions immediately.
- **Per-operator transfer rules.** PBT on each `Property.operators` entry: for any combination of input K-values, the output K-value satisfies the property's intended semantics (e.g., uniqueness propagation through `INNER JOIN` produces a key iff both sides contribute keys covering the join condition).
- **Per-aggregate transfer rules.** PBT on `Property.aggregates`: for representative input shapes, the output annotation matches the aggregate's known behaviour over `K`.
- **Jaffle goldens.** Per-column annotation snapshots on the jaffle fixture for each property, so refactors of the propagator do not silently change shape.
- **dbt cross-check.** Newer dbt versions ship column-level lineage; where available, we cross-check our `ColumnLineageGraph` against dbt's on a subset of models. Best-effort, not a contract.

## What this design does not cover

- Type inference of column SQL types from operations. The audit operates on type-light AST and that suffices for this substrate.
- User-domain transfer-rule authoring for the semantic-types property. Belongs to the types layer that consumes this substrate.
- A lineage UI or export format. Separate work if and when it is asked for.

## Sequencing

1. `Semiring[K]` and `Property[K]` API, plus the `Propagator`. No properties wired yet; only laws-testable scaffolding.
2. Per-model `ColumnLineageGraph` builder on `sqlglot.lineage`. Cache and reuse per-model SQL.
3. Cross-model composition across the manifest DAG.
4. First instantiated property: bag cardinality (`N` semiring). Wires the cross-model fanout propagation detector.
5. Uniqueness as candidate keys. Replaces the multi-source bail in the existing uniqueness layer.
6. Where-provenance and nullability, together with the `NOT IN`-nullable-upstream detector.
7. Publish the public API in `docs/current_state/` once the surface settles. Semantic tags land in this slot when the types layer arrives.

## References

- Green, T. J., Karvounarakis, G., Tannen, V. (2007). *Provenance Semirings*. PODS. The K-relations framework this design is built on.
- Amsterdamer, Y., Deutch, D., Tannen, V. (2011). *Provenance for Aggregate Queries*. PODS. The semimodule extension used for aggregate transfer rules.
- sqlglot's `lineage` module: the per-model where-provenance primitive we layer on.
