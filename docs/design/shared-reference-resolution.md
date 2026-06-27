# Shared reference resolution

## Motivation

Resolving a name or column reference in compiled SQL to the graph entity it
denotes is a single capability, but it is currently implemented more than once.
The column-lineage builder is the authoritative resolver: it qualifies each
model, builds sqlglot scopes, and resolves every projection column through CTE,
derived-table, and union scopes to a `ColumnRef`. The detectors, running over a
separately parsed tree, re-derive a subset of that lexical reasoning by hand:
the uniqueness detector walks the enclosing `WITH` chain (`_cte_body_for`) and
maps FROM/JOIN aliases to relations, and the inner-flatten detector resolves an
`UNNEST` argument's qualifier the same way and then gives up at a CTE boundary.

The name-level version of this duplication was already consolidated: the dbt
name convention now has one owner (`build_name_to_source`) that the detectors
compose through `index_by_name` rather than re-encode. This document is the same
move one layer down, at the level of a *reference inside a parsed tree*: let the
detectors consume the builder's resolution directly instead of each re-walking
lexical scope.

The payoff is concrete. The inner-flatten detector resolves an `UNNEST(col)`
only when `col` is qualified by a top-level relation; an `UNNEST` reading a
column through a CTE (the dominant real-world shape, e.g. the platform-metrics
staging models) is left unresolved, so the detector cannot consult the
`array_nonemptiness` annotation the property already computed for that
CTE-internal column. A shared resolver closes that gap for free, because the
builder already resolves through CTE scopes.

## Why the builder's resolution does not reach detectors today

`_walk_model` copies the tree before working on it (`builder.py`):

```python
expression = tree.copy() if tree is not None else parse_sql(sql, dialect=dialect)
expression = qualify(expression, ...)   # rewrites in place: qualifies columns, expands *
```

The copy is load-bearing. `qualify` mutates structure (it stamps table
qualifiers onto columns, expands `SELECT *`, normalises aliases), and the
detectors match on the raw, un-qualified shape the audit parsed. Resolving
without copying would hand every detector a rewritten tree. So the resolved
`ColumnRef` stamps (`attach_column_ref`) land on the builder's private qualified
copy, whose node identities differ from the detector's tree, and the resolution
never crosses back.

## The mechanism: write resolution back onto the shared tree

sqlglot's `Expression.copy()` and `qualify` both preserve `Expression.meta` on
the column nodes that survive qualification (verified in
`tests/lineage/test_reference_resolution.py`). That gives a stable
correspondence across the copy:

1. Before copying, stamp each column and table on the *original* (the audit's
   shared) tree with an identity tag in `.meta`.
2. Copy, qualify, build scopes, and resolve, exactly as the builder does now.
3. For each reference resolved on the copy, read its identity tag and write the
   resolved `ColumnRef` / `SourceRef` back onto the *original* node.

Detectors then read the resolved ref off their own tree with the existing
`attach_column_ref` / `_column_ref_meta` accessors, no scope walking required.
The original tree stays un-qualified, so structural matching and finding line
numbers are unchanged; only `.meta` is enriched.

Nodes `qualify` adds (an expanded `SELECT *` column) carry no original tag and
simply have nothing to write back to, which is correct: the detector's tree has
no such node either.

## A constraint the spike surfaced

The builder resolves only the columns it needs for the graph: the ones in
projections. Detectors need references in other positions, an `UNNEST`
argument in a FROM arm, a join key, a `WHERE` column. So "consume the builder's
resolution" understates the target. The unifying primitive is a resolver that
resolves *every* column and relation reference in a tree against its scopes, and
both the builder (for projections) and the detectors (for their positions)
consume it. The builder's graph construction becomes a reader of the resolution
rather than the place resolution happens.

## Target shape

One resolver, computed once per model and shared:

```
resolve(tree, name_to_source, schema, dialect) -> Resolution
# Resolution answers: for an original column node, its ColumnRef;
#                     for an original table node, its SourceRef.
```

`Resolution` is keyed to the original tree's nodes (via the `.meta` tag during
construction, exposed as the existing `_column_ref_meta` read after write-back).
The audit walker runs `resolve` once per model on the trees it already parsed,
the column-graph builder consumes it to build edges, and every detector consumes
it instead of re-deriving scope.

## Staged plan

Each stage is behaviour-preserving and independently landable.

1. **Pin the mechanism.** A characterisation test for the sqlglot behaviour the
   design rests on (meta survives copy and qualify; the original stays
   un-qualified). Already in `tests/lineage/test_reference_resolution.py`.
2. **Extract the resolver core** from `_Walker`: the qualify + `build_scope` +
   `_resolve_column` and scope-source bookkeeping, producing a node-keyed
   `Resolution` and writing refs back onto the original nodes. The builder
   consumes its own extraction; the graph is byte-for-byte the same.
3. **Resolve all reference positions**, not just projection columns, so a
   detector can resolve an `UNNEST` argument or a join key.
4. **Migrate the inner-flatten detector** to read stamped refs. This deletes its
   `_relation_names` / `cte_names` resolution and makes it CTE-aware: an
   `UNNEST` of a CTE-internal column now consults the propagated
   `array_nonemptiness` annotation.
5. **Reassessed: leave the uniqueness scope index where it is.** On inspection
   `relation_scope_keys` / `activated_scope_keys` run the uniqueness *relation
   algebra* (`_RelationWalk`, bottom-up candidate-key inference with the join,
   group, and union key rules) over one tree. That is property-specific key
   computation, not a re-derivation of the builder's column resolution. The only
   lexical piece, `_cte_body_for`, exists to key that property-specific scope
   index by CTE body, so routing it through the column resolver would not delete
   the scope index, only swap one small `WITH` walk for a relation-resolution
   facility the resolver does not yet expose. Folding the key algebra into the
   shared resolver would be over-generalisation (the same smell that motivated
   keeping per-scope fact computation property-local in the name-resolution
   cleanup), so the uniqueness scope index stays as it is. The shared duplication
   it once had, the name convention, was already removed by `index_by_name`.

## Outcome

Stages 1 through 4 land the goal: one resolver (the builder) computes column and
relation identity, writes it onto the shared tree, and the inner-flatten detector
consumes it with zero re-derivation, gaining CTE-awareness as a result. Stage 5's
target turned out to be property-specific rather than shared, so it is left in
place by design rather than forced through the resolver.

## Risks and how each is contained

- **A model that fails to qualify.** The builder already tolerates this per
  model and records a `BuildIssue`; the resolver degrades the same way, leaving
  that model's references unstamped. A detector reading an absent stamp treats it
  as "unknown" (its existing silent-when-unsure posture), so a resolution miss
  never crashes a scan, it only forgoes a clear.
- **Resolution quality depends on the column schema.** Absent schema, some
  columns resolve blind today and would stay blind; the change moves no model
  from resolved to blind.
- **Finding line numbers.** Detectors keep matching on the raw tree, so the spans
  they report are unchanged; only `.meta` is enriched.
- **Double work.** The audit already parses each tree once and the builder
  already qualifies each model once. Sharing one `Resolution` removes the
  detectors' redundant per-tree scope walks rather than adding a pass.
```
