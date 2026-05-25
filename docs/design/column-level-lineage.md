# Column-level lineage

## Motivation

Three downstream needs converge on a single missing substrate.

1. **Multi-source uniqueness propagation.** The current uniqueness layer bails on join scopes (see `src/dblect/uniqueness/detector.py:51`). With per-column lineage we can attribute each output column to its source columns and propagate uniqueness through projections that preserve key-tuple structure.
2. **Cross-model fanout impact.** Detecting fanout at the join site is already done. Tracing which downstream columns carry the multiplied counts forward, and which aggregates become contaminated by them, is the next layer.
3. **Substrate for semantic types.** When the semantic-types layer ships, tag tracking ("this column is `RevenuePreTax`") follows the same lineage edges. Building the substrate now means tag tracking layers on cleanly rather than requiring a parallel walk.

Building lineage as its own primitive, rather than re-deriving it inside each detector, also keeps the existing detector surface stable: detectors consume lineage facts the same way they consume uniqueness facts today.

## What sqlglot gives us

`sqlglot.lineage.lineage(column, sql, schema, sources, dialect, on_node)` is the primitive:

- With `column=None` it returns a `dict[str, Node]` mapping every top-level output column to its lineage root, with a shared cache across columns.
- Each `Node` carries the originating `sqlglot.expressions.Expression`, the source the column draws from, and pointers to downstream input nodes.
- The `on_node` callback fires for every node as the walk populates its downstream; we can attach our own payload there.
- `sources` lets us pass in other models' SQL strings, so cross-model lineage composes by threading dependencies through this argument.
- `dialect` is propagated to the parse/qualify pass, so dialect-specific column resolution works the same way the rest of the analyser does.

What it does not give us:

- A first-class cross-model lineage object. The DAG walk is ours.
- Operator classification (pass-through / transformed / aggregated / synthesized). We compute that from the lineage `Node`'s expression.
- Stable identity for the lineage graph. We key on `(model_uid, column_name)`.
- Robustness when sqlglot cannot fully resolve a column (dynamic SQL, dialect constructs sqlglot misparses, leftover Jinja that escaped dbt rendering). The lineage `Node` for those cases is shallow or absent.

## What we layer on top

### Per-model lineage

For each model we build a `ColumnLineage` from a single `sqlglot.lineage(None, compiled_sql, schema, sources, dialect)` call (one call per model, not per column, so the shared cache pays off). The result is stashed in the audit context keyed by `model_uid`.

```python
@dataclass(frozen=True, slots=True)
class ColumnLineage:
    model_uid: str
    columns: Mapping[str, ColumnOrigin]


@dataclass(frozen=True, slots=True)
class ColumnOrigin:
    output: str                          # column name on this model's output
    op: ColumnOp                         # how the value was produced
    inputs: tuple[ColumnRef, ...]        # source columns this output draws from
    expression_sql: str                  # the SQL expression that produced this column
    line_span: tuple[int, int] | None    # location in compiled SQL


@dataclass(frozen=True, slots=True)
class ColumnRef:
    source: SourceRef                    # model_uid, source_uid, or CTE name in scope
    column: str


class ColumnOp(StrEnum):
    PASS_THROUGH = "pass_through"        # bare Column reference, no wrapping expression
    TRANSFORMED  = "transformed"         # arithmetic, CASE, scalar function, cast
    AGGREGATED   = "aggregated"          # SUM/COUNT/MIN/MAX/AVG/ARRAY_AGG/...
    SYNTHESIZED  = "synthesized"         # literals, current_timestamp(), now(), random()
    UNKNOWN      = "unknown"             # sqlglot resolved partially; lineage is incomplete
```

`ColumnOp` is derived by inspecting the lineage `Node`'s expression:

- `PASS_THROUGH`: the projection is a bare `exp.Column` (optionally aliased).
- `AGGREGATED`: the projection is wrapped in an `exp.AggFunc` subclass.
- `SYNTHESIZED`: the projection has no `exp.Column` descendants.
- `TRANSFORMED`: anything else with at least one `exp.Column` descendant.
- `UNKNOWN`: sqlglot returned no usable `Node` for this output, or returned one whose source attribution we cannot resolve.

### Cross-model lineage graph

Across the DAG, lineage is a graph keyed by `ColumnRef`.

```python
@dataclass(frozen=True, slots=True)
class LineageGraph:
    edges: Mapping[ColumnRef, tuple[ColumnRef, ...]]   # downstream column -> upstream columns
    origins: Mapping[ColumnRef, ColumnOrigin]
```

Construction is topological:

1. Walk the DAG in dependency order.
2. For each model, populate `sources` with the SQL of every model it depends on (cached, not re-rendered each time).
3. Call `sqlglot.lineage(None, ...)` once.
4. Translate each output `Node` into a `ColumnOrigin`; emit edges per `inputs` entry. When an input refers to a source or seed, the edge terminates there (sources do not have outgoing lineage).
5. Merge into the audit-wide `LineageGraph`.

The cache is per-audit: `LineageGraph` is rebuilt from scratch on each `dblect audit` run, same as the manifest is reparsed.

## Use sites

### Uniqueness propagation upgrade

The multi-source bail in the current uniqueness propagation (`src/dblect/uniqueness/detector.py:51`) goes away. For a join with sources A and B, the output's uniqueness facts derive from per-column lineage tagged with the join condition. A key over `(a_id, b_id)` is unique iff each component traces back to a unique key on its respective source. The propagation pass consumes lineage the same way it consumes uniqueness facts today.

### New detectors that need lineage

- **Cross-model NOT IN with nullable upstream.** The NOT-IN-nullable-subquery detector (filed as its own issue) asks lineage whether the projected column came from an upstream that has a `not_null` fact.
- **Aggregate over already-aggregated column.** A `SUM(x)` where `x` came from a `SUM(...)` upstream usually double-counts. Lineage answers the "did this column come from an aggregate" question cleanly.
- **Cross-model fanout propagation.** A `join_fanout` finding on model M projects forward: any downstream column whose lineage flows through M's joined-in side inherits the fanout. The finding surfaces at the consumer, not just at M.

### Semantic types

When the semantic-types layer arrives, each declared type binds to `(model_uid, column)`. Tag propagation walks `LineageGraph.edges` forward: a type on an input column flows to its output under the same `op` classification. Pass-through preserves the type; aggregate transforms it via the aggregate's transfer rule (declared on the type); transformed needs either a per-operator rule or an explicit annotation; synthesized starts fresh.

## Failure modes

We will hit cases where lineage is incomplete. The contract is that detectors consume lineage as a partial map: where a column's origin is `UNKNOWN`, the lineage-dependent detector silently skips that column rather than producing a wrong answer.

- **Macros sqlglot cannot render.** dbt expands macros before we see the SQL, but custom Jinja that emits non-standard SQL (runtime-resolved table names, dynamic column lists) may leave artefacts sqlglot does not fully resolve. Lineage for affected columns is `UNKNOWN`; we surface a per-model "lineage incomplete" note in the audit report so users know which findings might be silenced.
- **Dialect-specific constructs.** Some dialects' lateral joins, `PIVOT`, `QUALIFY`, struct field access produce node trees with unclear source attribution. Same fallback: mark as `UNKNOWN`.
- **Identical column names from joined sources.** sqlglot's `Scope` qualification disambiguates these. Where qualification fails, the column gets `UNKNOWN`. (sqlglot's `qualify` is fairly robust; this is a rare path.)
- **Recursive CTEs and window functions referenced outside the projection.** Each gets a per-case decision: model conservatively (mark as `TRANSFORMED` with the full input set), or carve out and skip. We start conservative and tighten as concrete cases land.

The "incomplete" model means lineage is never load-bearing for correctness: detectors that need lineage and do not have it stay quiet rather than guess. False negatives are acceptable here; false positives from inferred-but-wrong lineage are not.

## Testing strategy

- **PBT on lineage invariants.** For any sqlglot-parseable generated SQL: every output column's lineage either traces to at least one input column or is `SYNTHESIZED`. No output ever has zero edges and a non-literal expression.
- **Golden tests on jaffle.** Fixed lineage assertions on each jaffle model so refactors of the builder do not silently change shape. Snapshot-style tests, easy to read, fail loudly on real regressions.
- **Differential check against dbt's column-level lineage.** Newer dbt versions ship column-level lineage; on fixtures where dbt's lineage is available we cross-check on a subset of models. Best-effort, not a contract.

## What this design does not cover

- Type inference (column SQL types from operations). The existing audit operates on a type-light AST and that is fine for the lineage substrate.
- Tag-propagation transfer rules for transformed ops. Those belong to the semantic-types layer.
- A lineage UI or export format. A separate ticket if and when it is needed.

## Sequencing

1. `ColumnLineage` data model and per-model builder using `sqlglot.lineage()`.
2. `LineageGraph` cross-model builder.
3. Plumb into the uniqueness propagation pass to close the multi-source gap.
4. Land the lineage-dependent detectors (NOT-IN-nullable-upstream, aggregate-over-aggregated, cross-model fanout propagation) as they unlock.
5. Document the public lineage API in `docs/current_state/` once the surface settles.

## Prior art and influences

- sqlglot's `lineage` module does the parse/qualify/walk heavy lifting; we are layering DAG composition and operator classification on top, not reinventing it.
- dbt's own column-level lineage (newer versions) covers a similar surface; their work is an excellent reference and we cross-check against it where available.
- Refinement-type and dependent-type literature on tag propagation through projection operators informs the eventual semantic-types layer that consumes this graph.
