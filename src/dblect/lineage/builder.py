"""Build ``ColumnLineageGraph`` from compiled model SQL.

The per-model builder calls ``sqlglot.lineage`` (which itself does the
parse + qualification + downstream walk) and translates the returned ``Node``
trees into our graph shape. The translation stamps each ``exp.Column`` in the
captured projection expression with the resolved ``ColumnRef`` of its
ultimate leaf source, so the propagator can walk the expression directly.

V0 scope: CTE intermediate columns are collapsed at translation time. Each
top-level ``exp.Column`` is stamped with the *set* of leaf ``ColumnRef``s
that the CTE intermediate's expression touched, walked across every
downstream branch in the lineage chain. Where-provenance recovers the full
leaf union by folding that set with the semiring's ``times`` at propagation
time. Properties that need per-CTE intermediate annotations (e.g.,
nullability across multi-step transforms in CTEs, where the operator
structure matters) still want a follow-up that materialises CTE columns as
their own graph entries.

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
from dblect.lineage.property import attach_column_refs
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
        except (KeyboardInterrupt, SystemExit):
            raise
        except SqlglotError as e:
            issues.append(BuildIssue(model_unique_id=uid, message=f"sqlglot: {e}"))
            continue
        except Exception as e:
            # sqlglot.lineage runs parse + qualifier + optimizer + walker; not
            # every failure on that path subclasses SqlglotError (KeyError on a
            # missing schema entry, AttributeError on an unexpected Expression
            # shape, RecursionError on pathological nesting). One bad model
            # shouldn't blank lineage for every downstream model.
            issues.append(BuildIssue(model_unique_id=uid, message=f"{type(e).__name__}: {e}"))
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
    """Stamp every ``exp.Column`` in ``expression`` with the set of leaf ``ColumnRef``s
    it resolves to, and return the union of those leaves.

    For each Column inside the expression, walk the lineage chain from the
    matching downstream ``Node`` to every Table-sourced leaf reachable from
    it. A Column that came from a CTE intermediate like ``a.x + a.y``
    resolves to both ``leaf.x`` and ``leaf.y``; the stamp records the full
    set so the propagator can fold them at walk time. Columns that don't
    resolve are left unstamped; the propagator treats them as "unknown" and
    falls back to its default.
    """
    downstream_by_name = _index_downstream(root)
    leaves: set[ColumnRef] = set()
    for col in expression.find_all(exp.Column):
        qual_name = _qualified_name(col)
        if qual_name is None:
            continue
        resolved = _resolve_to_leaves(qual_name, downstream_by_name)
        refs: set[ColumnRef] = set()
        for source_name, leaf_column in resolved:
            source_ref = name_to_source.get(source_name)
            if source_ref is None:
                continue
            refs.add(ColumnRef(source=source_ref, column=leaf_column.lower()))
        if not refs:
            continue
        attach_column_refs(col, frozenset(refs))
        leaves |= refs
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


def _resolve_to_leaves(qual_name: str, by_name: Mapping[str, Node]) -> frozenset[tuple[str, str]]:
    """Walk every branch from ``qual_name`` to its Table-sourced leaves.

    Each result is a ``(source_table_name, column_name)`` pair: the source
    table's real identifier off the leaf ``Node``'s ``exp.Table`` source
    (distinct from any FROM alias the lineage chain carried) and the column
    name parsed off the leaf's qualified ``.name``.

    A single qualified name can expand into multiple leaves when an
    intermediate column in the lineage chain (most commonly a CTE column or
    inline-subquery projection) was built from several upstream columns
    (``a.x + a.y AS combined``). The outer projection only sees one Column
    referencing that intermediate, so resolving has to fan out across every
    downstream branch rather than picking one arbitrarily.

    Only chain branches that terminate at an ``exp.Table`` contribute. A
    branch sqlglot couldn't trace to a real table (CTE alias the walker
    didn't expand, unqualified column reference, subquery whose lineage
    wasn't extracted) contributes nothing: the function returns absence
    rather than a guess shaped like a leaf. Where-provenance's empty
    annotation is the correct answer for "unresolved"; properties that need
    to track unresolved-ness explicitly should model it with their own
    sentinel rather than rely on a stand-in here.

    Returns the empty frozenset if no Table-sourced leaf is reachable.
    Cycles are tolerated by tracking visited names.
    """
    out: set[tuple[str, str]] = set()
    seen: set[str] = set()
    stack: list[str] = [qual_name]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        node = by_name.get(name)
        if node is None:
            continue
        if isinstance(node.source, exp.Table):
            split = _split_qualified(node.name)
            if split is not None:
                _, column = split
                out.add((node.source.name, column))
            continue
        stack.extend(nxt.name for nxt in node.downstream if nxt.name)
    return frozenset(out)


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

    Tables with no documented columns are omitted from the result rather than
    emitted as ``{}``. sqlglot.lineage rejects an empty column dict with
    ``Table must have at least one column``, which would kill the build for
    any model that transitively touches an undocumented seed or source. When
    the table is simply absent from the schema dict, sqlglot trusts the
    qualifiers already present in the SQL and lineage proceeds.
    """
    out: dict[str, dict[str, str]] = {}
    for src in manifest.sources.values():
        name = src.identifier or src.name
        for col_name, col in src.columns.items():
            out.setdefault(name, {})[col_name] = col.data_type or "UNKNOWN"
    for node in _models_seeds_snapshots(manifest):
        for col_name, col in node.columns.items():
            out.setdefault(node.name, {})[col_name] = col.data_type or "UNKNOWN"
    return out


def _models_seeds_snapshots(manifest: Manifest) -> Iterable[ManifestNode]:
    """All node-shaped manifest entries whose names can appear as table qualifiers."""
    yield from manifest.models.values()
    for node in manifest.nodes.values():
        if node.resource_type in (ResourceType.SEED, ResourceType.SNAPSHOT):
            yield node
