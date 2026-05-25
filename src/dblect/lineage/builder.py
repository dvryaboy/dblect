"""Build ``ColumnLineageGraph`` from compiled model SQL.

The per-model builder calls ``sqlglot.lineage`` (which itself does the
parse + qualification + downstream walk) and translates the returned ``Node``
trees into our graph shape. The translation stamps each ``exp.Column`` in the
captured projection expression with the resolved ``ColumnRef`` of its
ultimate leaf source, so the propagator can walk the expression directly.

V0 scope: CTE intermediate columns are collapsed (the leaf reference is
attached straight to the top-level Column). Properties that need per-CTE
intermediate annotations (nullability across multi-step transforms in CTEs)
will want a follow-up that materialises CTE columns as their own graph
entries. Where-provenance, which only unions leaves, does not need that.

Cross-model composition is a topological walk over the manifest DAG that
calls the per-model builder for each model and merges results. We deliberately
do not pass ``sources`` into ``sqlglot.lineage`` so the lineage walk stops at
each upstream model's boundary; the propagator stitches the annotations
together via the merged graph.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlglot import Expr
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node, lineage

from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceKind, SourceRef
from dblect.lineage.property import attach_column_ref
from dblect.manifest import Manifest, ResourceType
from dblect.manifest import Node as ManifestNode


@dataclass(frozen=True, slots=True)
class BuildIssue:
    """One non-fatal problem encountered while building lineage for a model.

    The builder collects these rather than raising so a single model's
    sqlglot failure does not blank out the whole audit graph. Callers can
    surface issues in the audit report.
    """

    model_unique_id: str
    message: str


@dataclass(frozen=True, slots=True)
class BuildResult:
    graph: ColumnLineageGraph
    issues: tuple[BuildIssue, ...]


def build_manifest_graph(
    manifest: Manifest,
    *,
    dialect: str | None = "duckdb",
) -> BuildResult:
    """Build the cross-model ``ColumnLineageGraph`` for every model in ``manifest``.

    Walks the manifest DAG in topological order. Each model's SQL is parsed
    independently via ``sqlglot.lineage``; the qualifier-to-source resolver is
    derived from the manifest so cross-model references land as edges to the
    upstream model's columns. Models without compiled SQL are skipped and
    reported in ``BuildResult.issues``.
    """
    name_to_source = _build_name_to_source(manifest)
    schema = _build_schema(manifest)
    issues: list[BuildIssue] = []
    graph = ColumnLineageGraph.empty()
    for uid in manifest.dag.topological_order():
        if uid not in manifest.models:
            continue
        model = manifest.models[uid]
        sql = model.analysis_sql
        if sql is None:
            issues.append(BuildIssue(model_unique_id=uid, message="model has no compiled SQL"))
            continue
        try:
            per_model = build_model_graph(
                model_uid=uid,
                sql=sql,
                name_to_source=name_to_source,
                schema=schema,
                dialect=dialect,
            )
        except SqlglotError as e:
            issues.append(BuildIssue(model_unique_id=uid, message=f"sqlglot: {e}"))
            continue
        graph = graph.merge(per_model)
    return BuildResult(graph=graph, issues=tuple(issues))


def build_model_graph(
    *,
    model_uid: str,
    sql: str,
    name_to_source: Mapping[str, SourceRef],
    schema: Mapping[str, Mapping[str, str]] | None = None,
    dialect: str | None = "duckdb",
) -> ColumnLineageGraph:
    """Build the lineage graph entries for one model's output columns.

    Calls ``sqlglot.lineage(None, sql, schema=schema, dialect=dialect)`` and
    translates the resulting ``dict[str, Node]`` into a ``ColumnLineageGraph``.
    For each top-level output column, the projection expression is stored on
    the graph with each ``exp.Column`` stamped with a ``ColumnRef`` pointing
    at the leaf upstream column. Edges are the flattened set of leaves.

    `name_to_source` resolves the qualifier that appears on ``exp.Column``s
    (typically a model name, source identifier, or CTE alias) to a
    ``SourceRef``. CTE aliases never have a manifest entry; their columns are
    collapsed to leaf references via the downstream walk before stamping.
    """
    self_ref = SourceRef(kind=SourceKind.MODEL, unique_id=model_uid)
    nodes = lineage(None, sql, schema=schema, dialect=dialect)  # type: ignore[arg-type]
    edges: dict[ColumnRef, frozenset[ColumnRef]] = {}
    expressions: dict[ColumnRef, Expr] = {}
    for output_col, root in nodes.items():
        output_ref = ColumnRef(source=self_ref, column=output_col.lower())
        expression = root.expression
        leaves = _stamp_and_collect(expression, root, name_to_source=name_to_source)
        expressions[output_ref] = expression
        edges[output_ref] = leaves
    return ColumnLineageGraph(edges=edges, expressions=expressions)


def _stamp_and_collect(
    expression: Expr,
    root: Node,
    *,
    name_to_source: Mapping[str, SourceRef],
) -> frozenset[ColumnRef]:
    """Stamp every ``exp.Column`` in ``expression`` with its leaf ``ColumnRef`` and return the leaves.

    For each Column inside the expression, look up the matching downstream
    ``Node`` in ``root`` (matched by qualified name), walk to a leaf source,
    read the leaf's column name and source-table identifier directly off the
    leaf ``Node`` (its ``name`` carries the column, its ``source`` is the
    ``exp.Table`` whose ``.name`` is the real table identifier, distinct from
    any FROM alias). Columns that don't resolve are left unstamped; the
    propagator treats them as "unknown" and falls back to its default.
    """
    downstream_by_name = _index_downstream(root)
    leaves: set[ColumnRef] = set()
    for col in expression.find_all(exp.Column):
        qual_name = _qualified_name(col)
        if qual_name is None:
            continue
        leaf = _resolve_to_leaf(qual_name, downstream_by_name)
        if leaf is None:
            continue
        source_name, leaf_column = leaf
        source_ref = name_to_source.get(source_name)
        if source_ref is None:
            continue
        leaf_ref = ColumnRef(source=source_ref, column=leaf_column.lower())
        attach_column_ref(col, leaf_ref)
        leaves.add(leaf_ref)
    return frozenset(leaves)


def _index_downstream(root: Node) -> dict[str, Node]:
    """Flatten ``root``'s downstream walk into a name -> Node map.

    Each ``Node`` carries a qualified ``name`` like ``"alias.column"``. This
    map lets the stamper find the chain entry for a given qualified column
    reference in the top expression.
    """
    out: dict[str, Node] = {}
    stack: list[Node] = list(root.downstream)
    while stack:
        node = stack.pop()
        if node.name and node.name not in out:
            out[node.name] = node
            stack.extend(node.downstream)
    return out


def _resolve_to_leaf(qual_name: str, by_name: Mapping[str, Node]) -> tuple[str, str] | None:
    """Walk from ``qual_name`` down the chain until reaching a Table-sourced leaf.

    Returns ``(source_table_name, column_name)`` taken from the leaf ``Node``:
    the source table's real identifier (off the ``exp.Table``'s ``.name``,
    which is distinct from any FROM alias the lineage chain carried) and the
    column name parsed off the leaf's qualified ``.name``. Returns ``None``
    if the name isn't in the chain, or if the chain doesn't terminate at a
    Table source within a reasonable bound.
    """
    seen: set[str] = set()
    name = qual_name
    while True:
        if name in seen:
            return None
        seen.add(name)
        node = by_name.get(name)
        if node is None:
            # Not in the downstream chain: probably a column reference that
            # sqlglot couldn't qualify. Try splitting the name as a best effort.
            return _split_qualified(name)
        if isinstance(node.source, exp.Table):
            # Leaf: real source identifier off the Table node, column off the
            # qualified Node.name.
            split = _split_qualified(node.name)
            if split is None:
                return None
            _, column = split
            return node.source.name, column
        if not node.downstream:
            return _split_qualified(node.name)
        # Otherwise, descend. Each intermediate has typically one downstream
        # pointer per upstream column; for a column with a single upstream we
        # follow that. For multiple, we can't pick one uniquely (the column
        # expression involved multiple sources); the caller's `find_all` walk
        # will visit each one separately, so we just follow the first here.
        nxt = node.downstream[0]
        name = nxt.name
        if not name:
            return None


def _qualified_name(col: exp.Column) -> str | None:
    """Render an ``exp.Column`` as ``"<qualifier>.<column>"`` if both parts are present."""
    table = col.table
    name = col.name
    if not table or not name:
        return None
    return f"{table}.{name}"


def _split_qualified(name: str) -> tuple[str, str] | None:
    """Split ``"qual.col"`` into ``(qual, col)``. Returns ``None`` if there's no dot."""
    if "." not in name:
        return None
    qual, _, col = name.rpartition(".")
    if not qual or not col:
        return None
    return qual, col


def _build_name_to_source(manifest: Manifest) -> Mapping[str, SourceRef]:
    """Map every name that can appear as a table qualifier to its ``SourceRef``.

    Includes models (by ``name``), sources (by ``identifier or name`` since
    dbt compiles ``{{ source(...) }}`` to ``identifier``), and seeds. On a
    name collision, models win, matching the convention that ``ref('x')``
    refers to a model named ``x`` over a source that happens to share it.
    """
    out: dict[str, SourceRef] = {}
    for uid, src in manifest.sources.items():
        out.setdefault(src.identifier or src.name, SourceRef(SourceKind.SOURCE, uid))
    for uid, node in manifest.nodes.items():
        if node.resource_type is ResourceType.SEED:
            out[node.name] = SourceRef(SourceKind.SEED, uid)
        elif node.resource_type is ResourceType.SNAPSHOT:
            out[node.name] = SourceRef(SourceKind.SNAPSHOT, uid)
    for uid, model in manifest.models.items():
        out[model.name] = SourceRef(SourceKind.MODEL, uid)
    return out


def _build_schema(manifest: Manifest) -> Mapping[str, Mapping[str, str]]:
    """Schema dict for ``sqlglot.lineage``: ``{table_name: {column: type}}``.

    Lets sqlglot qualify columns and walk lineage cleanly. Type strings come
    from manifest column metadata; missing types default to ``UNKNOWN`` (a
    sqlglot-accepted placeholder).
    """
    out: dict[str, dict[str, str]] = {}
    for src in manifest.sources.values():
        name = src.identifier or src.name
        out.setdefault(name, {})
        for col_name, col in src.columns.items():
            out[name][col_name] = col.data_type or "UNKNOWN"
    for node in _models_seeds_snapshots(manifest):
        out.setdefault(node.name, {})
        for col_name, col in node.columns.items():
            out[node.name][col_name] = col.data_type or "UNKNOWN"
    return out


def _models_seeds_snapshots(manifest: Manifest) -> Iterable[ManifestNode]:
    """All node-shaped manifest entries whose names can appear as table qualifiers."""
    yield from manifest.models.values()
    for node in manifest.nodes.values():
        if node.resource_type in (ResourceType.SEED, ResourceType.SNAPSHOT):
            yield node
