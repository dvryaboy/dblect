"""Build ``ColumnLineageGraph`` from compiled model SQL.

For each model we parse + qualify the SQL with sqlglot, walk the scope
tree, and register one ``ColumnRef`` per "interesting" projection:

* The top-level SELECT's projections become ``ColumnRef``s on the model.
* CTE and inline-subquery projections become ``ColumnRef``s on a
  synthetic ``cte.<model_uid>.<scope_path>`` source.
* UNION ALL combined output columns become ``ColumnRef``s on a synthetic
  ``union.<model_uid>.<scope_path>.<col>`` source whose expression is a
  ``UnionConfluence`` carrying the per-arm ``ColumnRef``s. Each arm is
  itself a ``ColumnRef`` on ``union.<...>#<arm_index>``.

Each ``exp.Column`` inside any projection is stamped with the single
immediate-upstream ``ColumnRef`` the qualifier resolves to. The propagator
walks at an ``exp.Column`` into that one upstream; structural fan-out
(CTE expressions, UNION arms) lives in the graph as separate nodes.

Cross-model composition is a topological walk that calls the per-model
builder and merges results. The per-model build stops at upstream-model
boundaries: a column qualified by an upstream model name resolves to that
model's ``ColumnRef`` (kind ``MODEL``) rather than recursing into the
upstream model's SQL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import cast

import sqlglot
from sqlglot import Expr
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, build_scope

from dblect.lineage.graph import (
    AggregationSite,
    ColumnLineageGraph,
    ColumnRef,
    RelationLineageGraph,
    SourceKind,
    SourceRef,
    attach_aggregation_site,
    attach_source_ref,
)
from dblect.lineage.property import UnionConfluence, attach_column_ref
from dblect.manifest import Manifest, ResourceType
from dblect.manifest import Node as ManifestNode
from dblect.sql import SQLParseError, parse_sql
from dblect.sql import _sqlglot as sg


@dataclass(frozen=True, slots=True)
class BuildIssue:
    """One non-fatal problem encountered while building lineage for a model.

    Collected rather than raised so a single model's failure does not
    blank out the audit graph; callers surface them in the report.
    """

    model_unique_id: str
    message: str


@dataclass(frozen=True, slots=True)
class BuildResult:
    graph: ColumnLineageGraph
    issues: tuple[BuildIssue, ...]


@dataclass(frozen=True, slots=True)
class RelationBuildResult:
    graph: RelationLineageGraph
    issues: tuple[BuildIssue, ...]


def build_relation_graph(
    manifest: Manifest,
    *,
    dialect: str | None = "duckdb",
    parsed: Mapping[str, Expr] | None = None,
) -> RelationBuildResult:
    """Build the cross-model ``RelationLineageGraph`` for relation-scoped
    propagation (uniqueness).

    Each model's compiled SQL is parsed and its upstream ``exp.Table`` references
    are stamped with the ``SourceRef`` they resolve to, so the relation reducer
    recurses across model boundaries via the propagator's shared ``recurse`` rather
    than re-resolving names. ``parsed`` lets a caller share already-parsed trees
    (the audit walker does) so the SQL is parsed once; the trees are stamped in
    place. A model with no compiled SQL or a parse error is reported in ``issues``
    and left out of the graph, so one bad model does not blank the rest. Sources
    and seeds carry no derivation; they enter only as recursion targets that
    ground from facts.
    """
    name_to_source = _build_name_to_source(manifest)
    derivations: dict[SourceRef, Expr] = {}
    issues: list[BuildIssue] = []
    for uid, model in manifest.models.items():
        if parsed is not None:
            tree = parsed.get(uid)
            if tree is None:
                issues.append(BuildIssue(model_unique_id=uid, message="model has no parsed SQL"))
                continue
        else:
            sql = model.analysis_sql
            if sql is None:
                issues.append(BuildIssue(model_unique_id=uid, message="model has no compiled SQL"))
                continue
            try:
                tree = parse_sql(sql, dialect=dialect)
            except SQLParseError as e:
                issues.append(BuildIssue(model_unique_id=uid, message=f"parse error: {e}"))
                continue
        _stamp_tables(tree, name_to_source)
        derivations[SourceRef(kind=SourceKind.MODEL, unique_id=uid)] = tree
    return RelationBuildResult(graph=RelationLineageGraph(derivations), issues=tuple(issues))


def _stamp_tables(tree: Expr, name_to_source: Mapping[str, SourceRef]) -> None:
    """Stamp every upstream table reference with its ``SourceRef``.

    Naive by name: a reference whose rightmost name matches a manifest relation is
    stamped. A local CTE that shadows a relation name is also stamped, but the
    reducer consults its CTE scope before reading a stamp, so the shadow wins.
    """
    for table in tree.find_all(exp.Table):
        ref = name_to_source.get(table.name)
        if ref is not None:
            attach_source_ref(table, ref)


def build_manifest_graph(
    manifest: Manifest,
    *,
    dialect: str | None = "duckdb",
    parsed: Mapping[str, Expr] | None = None,
) -> BuildResult:
    """Build the cross-model ``ColumnLineageGraph`` for every model in ``manifest``.

    Walks the manifest DAG in topological order. Models without compiled
    SQL are skipped and reported in ``BuildResult.issues``. ``parsed`` lets a caller
    share already-parsed trees so the SQL is parsed once (the audit walker does).
    """
    name_to_source = _build_name_to_source(manifest)
    # The schema starts from documented columns (the only source for DAG leaves,
    # which have no SQL of their own) and grows as the walk proceeds: a model's
    # resolved output columns are folded in before its dependents are qualified.
    # Topological order is what makes that sound, so a `select *` or a qualified
    # reference resolves against an upstream model the project never documented.
    schema: dict[str, dict[str, str]] = {k: dict(v) for k, v in _build_schema(manifest).items()}
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
                tree=parsed.get(uid) if parsed is not None else None,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except SqlglotError as e:
            issues.append(BuildIssue(model_unique_id=uid, message=f"sqlglot: {e}"))
            continue
        except Exception as e:
            # Parse + qualify + scope-build is a deep call chain through
            # sqlglot; not every failure subclasses SqlglotError (KeyError,
            # AttributeError, RecursionError). One bad model shouldn't
            # blank lineage for every downstream model.
            issues.append(BuildIssue(model_unique_id=uid, message=f"{type(e).__name__}: {e}"))
            continue
        graph = graph.merge(per_model)
        _record_output_columns(schema, model.name, uid, per_model)
    return BuildResult(graph=graph, issues=tuple(issues))


def _record_output_columns(
    schema: dict[str, dict[str, str]], name: str, uid: str, per_model: ColumnLineageGraph
) -> None:
    """Fold a model's resolved output columns into the running schema so its
    dependents can qualify against them. The output columns are exactly the graph
    subjects keyed by the model's own ``SourceRef`` (its top-level projection; CTE
    and upstream subjects carry other refs). Types are irrelevant to column
    resolution, so they enter as ``UNKNOWN`` and never clobber a documented type."""
    self_ref = SourceRef(kind=SourceKind.MODEL, unique_id=uid)
    columns = schema.setdefault(name, {})
    for ref in per_model.subjects():
        if ref.source == self_ref:
            columns.setdefault(ref.column, "UNKNOWN")


def build_model_graph(
    *,
    model_uid: str,
    sql: str,
    name_to_source: Mapping[str, SourceRef],
    schema: Mapping[str, Mapping[str, str]] | None = None,
    dialect: str | None = "duckdb",
    tree: Expr | None = None,
) -> ColumnLineageGraph:
    """Build the lineage graph entries for one model: top-level output columns
    plus all materialised intermediates (CTEs, derived tables, UNION outputs).

    ``tree`` lets a caller share an already-parsed tree (the audit walker does) so the
    SQL is parsed once. It is copied before qualification, which mutates in place, so
    the caller's tree is left untouched.
    """
    self_ref = SourceRef(kind=SourceKind.MODEL, unique_id=model_uid)
    expression: Expr = tree.copy() if tree is not None else sqlglot.parse_one(sql, dialect=dialect)
    expression = qualify(
        expression,
        dialect=dialect,
        schema=cast("dict[str, object] | None", schema),
        validate_qualify_columns=False,
        identify=False,
    )
    root_scope = build_scope(expression)
    if root_scope is None:
        raise SqlglotError("Cannot build scope from SQL")

    walker = _Walker(model_uid=model_uid, self_ref=self_ref, name_to_source=name_to_source)
    walker.walk(root_scope, scope_path=())
    return ColumnLineageGraph(edges=walker.edges, expressions=walker.expressions)


class _Walker:
    """Per-model scope walker that builds graph entries as it descends."""

    def __init__(
        self,
        *,
        model_uid: str,
        self_ref: SourceRef,
        name_to_source: Mapping[str, SourceRef],
    ) -> None:
        self._model_uid = model_uid
        self._self_ref = self_ref
        self._name_to_source = name_to_source
        self.edges: dict[ColumnRef, frozenset[ColumnRef]] = {}
        self.expressions: dict[ColumnRef, Expr] = {}
        # Indices the resolver needs when stamping a Column whose qualifier
        # names a child scope: which SourceRef each child scope was assigned,
        # and which synthetic UNION combined-output SourceRef stands in for
        # each output column name of a union derived-table.
        self._scope_source_ref: dict[int, SourceRef] = {}
        self._union_output_ref: dict[tuple[int, str], SourceRef] = {}
        # A scalar subquery in a projection contributes the provenance of its
        # selected expression, resolved against its own scope. Keyed by the id of
        # the scope's inner expression (``exp.Subquery.this``) so the stamper can
        # find the scope for a subquery it meets inside a projection.
        self._subquery_scope_by_expr: dict[int, Scope] = {}
        # One AggregationSite per SELECT scope, resolved lazily and shared by every
        # aggregate stamped in that scope.
        self._site_by_scope: dict[int, AggregationSite | None] = {}

    def walk(
        self,
        scope: Scope,
        *,
        scope_path: tuple[str, ...],
        register_projections: bool = True,
    ) -> None:
        """Recursively walk ``scope``, registering each interesting projection.

        Child scopes (CTEs, derived tables, UNION arms) are assigned and
        registered before the parent's selects are stamped, so qualifiers
        like ``r.combined`` resolve to the right child SourceRef.

        ``register_projections=False`` descends into child scopes without
        registering this scope's own projections. ``_emit_union_nodes``
        uses this for UNION arms: the arm's projections are registered
        positionally under arm 0's output names rather than under the
        arm's own per-position aliases, so a single arm projection ends
        up in ``expressions`` once.
        """
        for cte_scope in scope.cte_scopes:
            cte_name = self._alias_for_child_scope(cte_scope, scope)
            if cte_name is None:
                continue
            cte_ref = SourceRef(
                kind=SourceKind.CTE,
                unique_id=self._synthetic_id("cte", scope_path, cte_name),
            )
            self._scope_source_ref[id(cte_scope)] = cte_ref
            self.walk(cte_scope, scope_path=(*scope_path, cte_name))

        for dt_scope in scope.derived_table_scopes:
            dt_alias = self._alias_for_child_scope(dt_scope, scope)
            if dt_alias is None:
                continue
            if isinstance(dt_scope.expression, exp.Union):
                self._register_derived_table_union(dt_scope, scope_path=(*scope_path, dt_alias))
            else:
                dt_ref = SourceRef(
                    kind=SourceKind.CTE,
                    unique_id=self._synthetic_id("cte", scope_path, dt_alias),
                )
                self._scope_source_ref[id(dt_scope)] = dt_ref
                self.walk(dt_scope, scope_path=(*scope_path, dt_alias))

        # Inline (non-derived-table, non-CTE) subquery scopes: EXISTS(...),
        # scalar subqueries in projections. We descend so nested CTEs and
        # derived tables get registered, but we never register the inline
        # subquery's own selects: it isn't a materialised intermediate, and
        # registering them under the parent's source ref would surface
        # phantom columns on whatever node the parent resolves to.
        for sub_scope in scope.subquery_scopes:
            self._subquery_scope_by_expr[id(sub_scope.expression)] = sub_scope
            self.walk(sub_scope, scope_path=scope_path, register_projections=False)

        scope_expr = scope.expression
        if isinstance(scope_expr, exp.Union):
            self._register_top_level_union(scope, scope_path=scope_path)
            return
        if not register_projections or not isinstance(scope_expr, exp.Selectable):
            return
        scope_source = self._source_ref_for_scope(scope)
        for select in scope_expr.selects:
            self._register_projection(select, scope=scope, scope_source=scope_source)

    def _register_projection(
        self,
        select: Expr,
        *,
        scope: Scope,
        scope_source: SourceRef,
    ) -> None:
        # A surviving ``Star`` means qualify couldn't expand ``SELECT *`` (no
        # schema for the source). Registering it would surface a phantom
        # ``"*"`` column on the scope's source.
        if isinstance(select, exp.Star):
            return
        out_name = self._alias_or_name(select)
        if not out_name:
            return
        col_ref = ColumnRef(source=scope_source, column=out_name.lower())
        immediate = self._stamp_columns(select, scope=scope)
        self._stamp_aggregates(select, scope=scope)
        self.expressions[col_ref] = select
        self.edges[col_ref] = immediate

    def _stamp_columns(self, expr: Expr, *, scope: Scope) -> frozenset[ColumnRef]:
        """Stamp the columns that supply ``expr``'s value with their upstream ``ColumnRef``.

        Columns lying directly in ``expr`` resolve against ``scope``. A scalar
        subquery contributes the provenance of its *selected* expression only: we
        recurse into its projection against the subquery's own scope and never
        touch its WHERE/FROM, so a correlated predicate column (which filters rows
        rather than supplying the value) does not leak in even though it may
        resolve against the outer scope. Returns the deduped set of refs as this
        projection's ``edges`` entry. An unresolved Column is silently skipped:
        the propagator reads an unstamped Column as "unknown" via the property
        default.
        """
        direct, subqueries = _projection_leaves(expr)
        immediate: set[ColumnRef] = set()
        for col in direct:
            ref = self._resolve_column(col, scope=scope)
            if ref is None:
                continue
            attach_column_ref(col, ref)
            immediate.add(ref)
        for subq in subqueries:
            inner = subq.this
            sub_scope = self._subquery_scope_by_expr.get(id(inner))
            if sub_scope is None or not isinstance(inner, exp.Selectable):
                continue
            for sel in inner.selects:
                if isinstance(sel, exp.Star):
                    continue
                immediate |= self._stamp_columns(sel, scope=sub_scope)
        return frozenset(immediate)

    def _resolve_column(self, col: exp.Column, *, scope: Scope) -> ColumnRef | None:
        table = col.table
        col_name = col.name
        if not table or not col_name:
            return None
        src = scope.sources.get(table)
        if src is None:
            return None
        if isinstance(src, exp.Table):
            source_ref = self._name_to_source.get(src.name)
            if source_ref is None:
                return None
            return ColumnRef(source=source_ref, column=col_name.lower())
        # ``scope.sources`` values are ``Table | Scope``, so by elimination
        # ``src`` is a Scope here. Union derived tables: the source is the
        # union's combined output, not the derived-table scope itself.
        union_ref = self._union_output_ref.get((id(src), col_name.lower()))
        if union_ref is not None:
            return ColumnRef(source=union_ref, column=col_name.lower())
        scope_ref = self._scope_source_ref.get(id(src))
        if scope_ref is None:
            return None
        return ColumnRef(source=scope_ref, column=col_name.lower())

    def _stamp_aggregates(self, projection: Expr, *, scope: Scope) -> None:
        """Stamp each aggregate call in ``projection`` with its scope's
        :class:`AggregationSite`, the context the coherence guard judges it in:
        the projection expression alone carries neither the GROUP BY, nor the
        relation being aggregated over, nor the scope's literal pins. An aggregate
        inside a window reduces per partition rather than per group, so it is left
        unstamped (the guard then stays silent-when-unproven) until window
        structure is read.
        """
        aggs = [a for a in projection.find_all(exp.AggFunc) if a.find_ancestor(exp.Window) is None]
        if not aggs:
            return
        site = self._aggregation_site(scope)
        if site is None:
            return
        for agg in aggs:
            attach_aggregation_site(agg, site)

    def _aggregation_site(self, scope: Scope) -> AggregationSite | None:
        key = id(scope)
        if key not in self._site_by_scope:
            sel = scope.expression
            self._site_by_scope[key] = (
                AggregationSite(
                    input_source=self._aggregation_input(sel, scope),
                    group_refs=self._group_refs(sel, scope),
                    pinned=self._pinned_refs(sel, scope),
                )
                if isinstance(sel, exp.Select)
                else None
            )
        return self._site_by_scope[key]

    def _aggregation_input(self, sel: exp.Select, scope: Scope) -> SourceRef | None:
        """The one relation the scope aggregates over: a join-free FROM of a single
        resolvable table (a manifest relation, or a CTE/derived table's synthetic
        ref). ``None`` closes the guard's dependency read, since the FD property
        has no scope to answer for."""
        if sg.joins_of(sel):
            return None
        from_ = sg.from_of(sel)
        if from_ is None or not isinstance(from_.this, exp.Table):
            return None
        src = scope.sources.get(from_.this.alias_or_name)
        if isinstance(src, exp.Table):
            return self._name_to_source.get(src.name)
        if src is not None:
            return self._scope_source_ref.get(id(src))
        return None

    def _group_refs(self, sel: exp.Select, scope: Scope) -> frozenset[ColumnRef] | None:
        """The GROUP BY columns resolved to their upstream refs; ``None`` for a
        group shape that is not all plain resolvable columns (positional or
        computed keys), which a guard must treat as unprovable."""
        group = sel.args.get("group")
        if not isinstance(group, exp.Group) or not group.expressions:
            return frozenset()
        out: set[ColumnRef] = set()
        for g in group.expressions:
            if not isinstance(g, exp.Column) or isinstance(g.this, exp.Star):
                return None
            ref = self._resolve_column(g, scope=scope)
            if ref is None:
                return None
            out.add(ref)
        return frozenset(out)

    def _pinned_refs(self, sel: exp.Select, scope: Scope) -> frozenset[ColumnRef]:
        """Columns the scope's own WHERE equates to a literal, constant across
        every group by construction. An unresolvable pin is dropped, never guessed."""
        where = sg.where_of(sel)
        if where is None or not isinstance(where.this, Expr):
            return frozenset()
        out: set[ColumnRef] = set()
        for col in sg.equality_literal_columns(where.this):
            ref = self._resolve_column(col, scope=scope)
            if ref is not None:
                out.add(ref)
        return frozenset(out)

    def _register_derived_table_union(
        self,
        dt_scope: Scope,
        *,
        scope_path: tuple[str, ...],
    ) -> None:
        def output_source(out_name: str) -> SourceRef:
            return SourceRef(
                kind=SourceKind.UNION,
                unique_id=self._synthetic_id_union_output(scope_path, out_name),
            )

        def on_registered(out_name: str, src: SourceRef) -> None:
            self._union_output_ref[(id(dt_scope), out_name.lower())] = src

        self._emit_union_nodes(
            dt_scope,
            scope_path=scope_path,
            output_source_for=output_source,
            on_output_registered=on_registered,
        )

    def _register_top_level_union(
        self,
        union_scope: Scope,
        *,
        scope_path: tuple[str, ...],
    ) -> None:
        # Combined output IS the model column; no synthetic UNION node needed.
        self._emit_union_nodes(
            union_scope,
            scope_path=scope_path,
            output_source_for=lambda _: self._self_ref,
            on_output_registered=None,
        )

    def _emit_union_nodes(
        self,
        union_scope: Scope,
        *,
        scope_path: tuple[str, ...],
        output_source_for: Callable[[str], SourceRef],
        on_output_registered: Callable[[str, SourceRef], None] | None,
    ) -> None:
        """Register arms and per-column combined-output ``UnionConfluence`` nodes.

        Output column names come from arm 0 (standard SQL); arms contribute
        positionally, so an arm that aliases position i differently still
        binds to the same output column. Arms shorter than arm 0 (malformed
        SQL) contribute nothing for the missing positions.
        """
        arm_scopes = list(union_scope.union_scopes)
        if not arm_scopes:
            return
        first_arm = arm_scopes[0].expression
        if not isinstance(first_arm, exp.Selectable):
            return
        output_names = [self._alias_or_name(s) for s in first_arm.selects]

        per_arm_selects: list[list[Expr]] = []
        for arm in arm_scopes:
            arm_expr = arm.expression
            per_arm_selects.append(
                list(arm_expr.selects) if isinstance(arm_expr, exp.Selectable) else []
            )

        arm_refs_per_col: list[list[ColumnRef]] = [[] for _ in output_names]
        for arm_idx, arm_scope in enumerate(arm_scopes):
            arm_path = (*scope_path, f"arm{arm_idx}")
            arm_ref = SourceRef(
                kind=SourceKind.UNION_ARM,
                unique_id=self._synthetic_id_union_arm(scope_path, arm_idx),
            )
            self._scope_source_ref[id(arm_scope)] = arm_ref
            self.walk(arm_scope, scope_path=arm_path, register_projections=False)
            arm_selects = per_arm_selects[arm_idx]
            for col_idx, out_name in enumerate(output_names):
                if not out_name or col_idx >= len(arm_selects):
                    continue
                arm_select = arm_selects[col_idx]
                arm_col_ref = ColumnRef(source=arm_ref, column=out_name.lower())
                arm_refs_per_col[col_idx].append(arm_col_ref)
                immediate = self._stamp_columns(arm_select, scope=arm_scope)
                self._stamp_aggregates(arm_select, scope=arm_scope)
                self.expressions[arm_col_ref] = arm_select
                self.edges[arm_col_ref] = immediate

        for col_idx, out_name in enumerate(output_names):
            if not out_name or not arm_refs_per_col[col_idx]:
                continue
            output_src = output_source_for(out_name)
            if on_output_registered is not None:
                on_output_registered(out_name, output_src)
            output_col_ref = ColumnRef(source=output_src, column=out_name.lower())
            arm_refs = tuple(arm_refs_per_col[col_idx])
            self.expressions[output_col_ref] = UnionConfluence(arm_refs)
            self.edges[output_col_ref] = frozenset(arm_refs)

    def _source_ref_for_scope(self, scope: Scope) -> SourceRef:
        ref = self._scope_source_ref.get(id(scope))
        if ref is not None:
            return ref
        if scope.scope_type == ScopeType.ROOT:
            return self._self_ref
        # Fallback for subquery scopes that don't materialise referenced
        # outputs: anchor on the model so entries remain discoverable.
        return self._self_ref

    def _synthetic_id(self, prefix: str, scope_path: tuple[str, ...], leaf: str) -> str:
        path_parts = (*scope_path, leaf)
        return f"{prefix}.{self._model_uid}.{'.'.join(path_parts)}"

    def _synthetic_id_union_arm(self, scope_path: tuple[str, ...], arm_idx: int) -> str:
        path = ".".join(scope_path) if scope_path else "__top__"
        return f"union.{self._model_uid}.{path}#{arm_idx}"

    def _synthetic_id_union_output(self, scope_path: tuple[str, ...], col: str) -> str:
        path = ".".join(scope_path) if scope_path else "__top__"
        return f"union.{self._model_uid}.{path}.{col}"

    @staticmethod
    def _alias_or_name(select: Expr) -> str:
        # ``alias_or_name`` returns the projection's alias if set, else the
        # bare column name. Cast through ``str`` because Star and similar
        # expressions return non-string values.
        name = getattr(select, "alias_or_name", "")
        return str(name) if name else ""

    @staticmethod
    def _alias_for_child_scope(child: Scope, parent: Scope) -> str | None:
        """Find an alias the parent scope uses for this child scope.

        A Scope can appear in ``parent.sources`` under multiple aliases (a
        CTE referenced twice with different aliases); we return the first
        match, which is the introducing alias for path purposes.
        """
        for alias, src in parent.sources.items():
            if src is child:
                return alias
        return None


def _projection_leaves(expr: Expr) -> tuple[list[exp.Column], list[exp.Subquery]]:
    """Split ``expr`` into the columns lying directly in it and the outermost
    scalar subqueries within it.

    The walk stops at each ``Column`` and ``Subquery``, so a subquery's interior
    is left for a separate pass against its own scope rather than being resolved
    against this one. That boundary is what keeps a subquery's WHERE/FROM columns
    out of the enclosing projection's provenance.
    """
    direct: list[exp.Column] = []
    subqueries: list[exp.Subquery] = []

    def visit(node: Expr) -> None:
        if isinstance(node, exp.Column):
            direct.append(node)
            return
        if isinstance(node, exp.Subquery):
            subqueries.append(node)
            return
        for value in node.args.values():
            if isinstance(value, Expr):
                visit(value)
            elif isinstance(value, list):
                for item in cast("list[object]", value):
                    if isinstance(item, Expr):
                        visit(item)

    visit(expr)
    return direct, subqueries


def _build_name_to_source(manifest: Manifest) -> Mapping[str, SourceRef]:
    """Map every name that can appear as a table qualifier to its ``SourceRef``.

    Includes models (by ``name``), sources (by ``identifier or name`` since
    dbt compiles ``{{ source(...) }}`` to ``identifier``), and seeds. On a
    name collision, models win — matching the convention that ``ref('x')``
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
    """Schema dict for sqlglot's qualifier: ``{table_name: {column: type}}``.

    Tables with no documented columns are omitted rather than emitted as
    ``{}``. The qualifier rejects an empty column dict, which would kill the
    build for any model that transitively touches an undocumented seed or
    source. With the table simply absent, sqlglot trusts the qualifiers
    already present in the SQL.
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
