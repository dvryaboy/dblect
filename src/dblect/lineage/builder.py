"""Build ``ColumnLineageGraph`` from compiled model SQL.

For each model we parse + qualify the SQL with sqlglot, walk the resulting
scope tree, and register one ``ColumnRef`` per "interesting" projection:

* The top-level SELECT's projections become ``ColumnRef``s on the model.
* CTE projections become ``ColumnRef``s on a synthetic ``cte.<model_uid>.<scope_path>``
  source. Inline (derived-table) subqueries are handled the same way, with
  the derived-table alias slotted into the scope path.
* UNION ALL output columns become ``ColumnRef``s on a synthetic
  ``union.<model_uid>.<scope_path>.<col>`` source whose projection
  expression is ``Union(arm0_col, arm1_col, ...)``. Each arm's projection
  is itself a separate ``ColumnRef`` on ``union.<...>.<col>#<arm_index>``.

Each ``exp.Column`` inside any projection expression is stamped (via
``attach_column_ref``) with the single immediate-upstream ``ColumnRef``
the qualifier resolves to in that scope. That single-ref invariant is
what makes the propagator walk uniform: at an ``exp.Column`` it recurses
into one upstream; structural fan-out (CTE expressions, UNION arms) lives
in the graph as its own nodes.

Per-model edges hold the immediate-upstream relation: one entry per
``ColumnRef`` the projection expression's ``exp.Column``s stamp to. The
propagator stitches longer chains by recursion.

Cross-model composition is a topological walk over the manifest DAG that
calls the per-model builder for each model and merges results. The
per-model build deliberately stops at upstream-model boundaries: a column
qualified by an upstream model name resolves to that model's
``ColumnRef`` (kind ``MODEL``) rather than recursing into the upstream
model's SQL.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import sqlglot
from sqlglot import Expr
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer import Scope, build_scope, qualify
from sqlglot.optimizer.scope import ScopeType

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
    independently; the qualifier-to-source resolver is derived from the
    manifest so cross-model references land as edges to the upstream
    model's columns. Models without compiled SQL are skipped and reported
    in ``BuildResult.issues``.
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
            # Parse + qualify + scope-build is a deep call chain through
            # sqlglot; not every failure subclasses SqlglotError (KeyError
            # on a missing schema entry, AttributeError on an unexpected
            # Expression shape, RecursionError on pathological nesting).
            # One bad model shouldn't blank lineage for every downstream
            # model.
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
    """Build the lineage graph entries for one model's output columns plus all
    materialised intermediates (CTEs, derived tables, UNION ALL outputs).
    """
    self_ref = SourceRef(kind=SourceKind.MODEL, unique_id=model_uid)
    expression = sqlglot.maybe_parse(sql, dialect=dialect, copy=True)
    expression = qualify.qualify(
        expression,
        dialect=dialect,
        schema=schema,
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
    """Per-model scope walker that builds graph entries as it descends.

    Holds the in-progress ``edges`` / ``expressions`` dicts plus a
    ``scope_to_source_ref`` map so columns qualified by a CTE/derived-table
    alias can be resolved to the right ``SourceRef`` when the parent scope
    stamps them.
    """

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
        # Two indices the resolver needs when stamping a Column whose
        # qualifier names a child scope: which SourceRef that child scope
        # was assigned, and (for union-derived-table cases) which synthetic
        # union-output SourceRef stands in for each output column name.
        self._scope_source_ref: dict[int, SourceRef] = {}
        self._union_output_ref: dict[tuple[int, str], SourceRef] = {}

    def walk(self, scope: Scope, *, scope_path: tuple[str, ...]) -> None:
        """Recursively walk ``scope``, registering each interesting projection.

        Order of operations matters: child scopes (CTEs, derived tables,
        UNION arms) must be assigned and registered *before* the parent
        scope's selects are stamped, because the stamping resolves
        qualifiers like ``r.combined`` against those child SourceRefs.
        """
        # CTE scopes: each becomes a kind=CTE SourceRef and recurses.
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

        # Derived-table scopes: each is either a plain inline subquery
        # (treat as a CTE-shaped intermediate) or a UNION-ALL derived
        # table (treat specially via _register_union).
        for dt_scope in scope.derived_table_scopes:
            dt_alias = self._alias_for_child_scope(dt_scope, scope)
            if dt_alias is None:
                continue
            if isinstance(dt_scope.expression, exp.SetOperation):
                self._register_union(dt_scope, scope_path=(*scope_path, dt_alias))
            else:
                dt_ref = SourceRef(
                    kind=SourceKind.CTE,
                    unique_id=self._synthetic_id("cte", scope_path, dt_alias),
                )
                self._scope_source_ref[id(dt_scope)] = dt_ref
                self.walk(dt_scope, scope_path=(*scope_path, dt_alias))

        # Inline (non-derived-table, non-CTE) subquery scopes:
        # ``EXISTS(SELECT ...)``, scalar subqueries in projections.
        # Their output columns aren't referenced by the outer query so they
        # don't need synthetic SourceRefs, but their inner Columns still
        # want stamping in case a property walks into them.
        for sub_scope in scope.subquery_scopes:
            self.walk(sub_scope, scope_path=scope_path)

        # Now the scope's own selects.
        scope_expr = scope.expression
        if isinstance(scope_expr, exp.SetOperation):
            # A top-level UNION ALL at the model level: there's no derived-table
            # alias, so the scope itself is the union. Register the arms and
            # synthesise per-output-column nodes anchored on the model.
            self._register_top_level_union(scope, scope_path=scope_path)
            return
        if not hasattr(scope_expr, "selects"):
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
        out_name = self._alias_or_name(select)
        if not out_name:
            return
        col_ref = ColumnRef(source=scope_source, column=out_name.lower())
        immediate = self._stamp_columns(select, scope=scope)
        self.expressions[col_ref] = select
        self.edges[col_ref] = immediate

    def _stamp_columns(self, expr: Expr, *, scope: Scope) -> frozenset[ColumnRef]:
        """Stamp every ``exp.Column`` in ``expr`` with its immediate upstream ``ColumnRef``.

        Returns the deduped set of those refs as this projection's ``edges``
        value. An unresolved Column (qualifier missing, or qualifier maps to
        nothing the builder can name) is silently skipped: the propagator
        will treat the unstamped Column as "unknown" and fall back to
        ``Property.default()``.
        """
        immediate: set[ColumnRef] = set()
        for col in expr.find_all(exp.Column):
            ref = self._resolve_column(col, scope=scope)
            if ref is None:
                continue
            attach_column_ref(col, ref)
            immediate.add(ref)
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
        if isinstance(src, Scope):
            # Union derived tables: the source is the union's *combined output*,
            # not the derived-table scope itself. Look up the synthetic union
            # ref keyed on (scope, column_name).
            union_ref = self._union_output_ref.get((id(src), col_name.lower()))
            if union_ref is not None:
                return ColumnRef(source=union_ref, column=col_name.lower())
            scope_ref = self._scope_source_ref.get(id(src))
            if scope_ref is None:
                return None
            return ColumnRef(source=scope_ref, column=col_name.lower())
        return None

    def _register_union(
        self,
        dt_scope: Scope,
        *,
        scope_path: tuple[str, ...],
    ) -> None:
        """Materialise a UNION-ALL derived table as synthetic per-column union nodes.

        For each output column name (taken from the first arm's projection
        list — UNION arms must agree on column count, and the first arm
        names them), invent:

        * One synthetic union output ``ColumnRef`` (kind ``UNION_ARM``,
          id ``union.<model>.<path>.<col>``) whose expression is a
          ``exp.Union`` over per-arm ``exp.Column``s. Each arm Column is
          stamped with its arm's ``ColumnRef`` so the propagator recurses
          naturally.
        * One per-arm ``ColumnRef`` (id ``union.<model>.<path>.<col>#<i>``)
          whose expression is the arm's actual projection.
        """
        arm_scopes = list(dt_scope.union_scopes)
        if not arm_scopes:
            return
        first_arm = arm_scopes[0].expression
        if not hasattr(first_arm, "selects"):
            return
        output_names = [self._alias_or_name(s) for s in first_arm.selects]

        # Index of arm output columns: per arm, name -> projection expression.
        # If an arm has fewer projections than the first (malformed SQL),
        # missing slots produce None entries that get skipped.
        per_arm_projections: list[dict[str, Expr]] = []
        for arm in arm_scopes:
            arm_expr = arm.expression
            if not hasattr(arm_expr, "selects"):
                per_arm_projections.append({})
                continue
            per_arm_projections.append(
                {self._alias_or_name(s): s for s in arm_expr.selects if self._alias_or_name(s)}
            )

        # Stamp each arm's columns against its own scope first, so the arms'
        # ColumnRefs are present when the union output gets synthesised.
        arm_refs_per_col: dict[str, list[ColumnRef]] = {name: [] for name in output_names if name}
        for arm_idx, arm_scope in enumerate(arm_scopes):
            arm_path = (*scope_path, f"arm{arm_idx}")
            arm_ref = SourceRef(
                kind=SourceKind.UNION_ARM,
                unique_id=self._synthetic_id_union_arm(scope_path, arm_idx),
            )
            self._scope_source_ref[id(arm_scope)] = arm_ref
            # Recurse so any nested CTEs/derived/subquery inside the arm get
            # registered before we stamp the arm's own selects.
            self.walk(arm_scope, scope_path=arm_path)
            # Register each arm projection as its own ColumnRef.
            for out_name in output_names:
                if not out_name:
                    continue
                arm_select = per_arm_projections[arm_idx].get(out_name)
                if arm_select is None:
                    continue
                arm_col_ref = ColumnRef(source=arm_ref, column=out_name.lower())
                arm_refs_per_col[out_name].append(arm_col_ref)
                # The arm's projection itself was already registered by the
                # nested walk()'s _register_projection call. If the walk
                # didn't register it (e.g., scope shape we didn't recognise),
                # do so now via a stamping pass.
                if arm_col_ref not in self.expressions:
                    immediate = self._stamp_columns(arm_select, scope=arm_scope)
                    self.expressions[arm_col_ref] = arm_select
                    self.edges[arm_col_ref] = immediate

        # Synthesise one union-output ColumnRef per output column name.
        for out_name in output_names:
            if not out_name:
                continue
            union_out_ref = SourceRef(
                kind=SourceKind.UNION_ARM,
                unique_id=self._synthetic_id_union_output(scope_path, out_name),
            )
            # Record (dt_scope, col_name) -> union_out_ref so the parent
            # scope's `u.<col>` references resolve to it.
            self._union_output_ref[(id(dt_scope), out_name.lower())] = union_out_ref
            union_col_ref = ColumnRef(source=union_out_ref, column=out_name.lower())
            arm_columns = [_synth_column_with_ref(r) for r in arm_refs_per_col[out_name]]
            if not arm_columns:
                continue
            union_expr = _build_union_expression(arm_columns)
            self.expressions[union_col_ref] = union_expr
            self.edges[union_col_ref] = frozenset(arm_refs_per_col[out_name])

    def _register_top_level_union(
        self,
        union_scope: Scope,
        *,
        scope_path: tuple[str, ...],
    ) -> None:
        """A bare ``SELECT ... UNION ALL SELECT ...`` at the model level.

        Each union output column maps directly to a model-level
        ``ColumnRef`` whose expression is the synthesised union; arms become
        their own ``UNION_ARM`` refs.
        """
        arm_scopes = list(union_scope.union_scopes)
        if not arm_scopes:
            return
        first_arm = arm_scopes[0].expression
        if not hasattr(first_arm, "selects"):
            return
        output_names = [self._alias_or_name(s) for s in first_arm.selects]

        per_arm_projections: list[dict[str, Expr]] = []
        for arm in arm_scopes:
            arm_expr = arm.expression
            if not hasattr(arm_expr, "selects"):
                per_arm_projections.append({})
                continue
            per_arm_projections.append(
                {self._alias_or_name(s): s for s in arm_expr.selects if self._alias_or_name(s)}
            )

        arm_refs_per_col: dict[str, list[ColumnRef]] = {name: [] for name in output_names if name}
        for arm_idx, arm_scope in enumerate(arm_scopes):
            arm_path = (*scope_path, f"arm{arm_idx}")
            arm_ref = SourceRef(
                kind=SourceKind.UNION_ARM,
                unique_id=self._synthetic_id_union_arm(scope_path, arm_idx),
            )
            self._scope_source_ref[id(arm_scope)] = arm_ref
            self.walk(arm_scope, scope_path=arm_path)
            for out_name in output_names:
                if not out_name:
                    continue
                arm_select = per_arm_projections[arm_idx].get(out_name)
                if arm_select is None:
                    continue
                arm_col_ref = ColumnRef(source=arm_ref, column=out_name.lower())
                arm_refs_per_col[out_name].append(arm_col_ref)
                if arm_col_ref not in self.expressions:
                    immediate = self._stamp_columns(arm_select, scope=arm_scope)
                    self.expressions[arm_col_ref] = arm_select
                    self.edges[arm_col_ref] = immediate

        for out_name in output_names:
            if not out_name:
                continue
            model_col_ref = ColumnRef(source=self._self_ref, column=out_name.lower())
            arm_columns = [_synth_column_with_ref(r) for r in arm_refs_per_col[out_name]]
            if not arm_columns:
                continue
            union_expr = _build_union_expression(arm_columns)
            self.expressions[model_col_ref] = union_expr
            self.edges[model_col_ref] = frozenset(arm_refs_per_col[out_name])

    def _source_ref_for_scope(self, scope: Scope) -> SourceRef:
        ref = self._scope_source_ref.get(id(scope))
        if ref is not None:
            return ref
        # Root scope: anchor on the model.
        if scope.scope_type == ScopeType.ROOT:
            return self._self_ref
        # Fallback (subquery scopes etc. that don't materialise outputs):
        # treat as a model-anchored ref. The selects from such scopes
        # aren't referenced elsewhere, so the SourceRef just keeps them
        # discoverable in the graph for debugging.
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
        # ``alias_or_name`` is the sqlglot accessor that returns the projection's
        # alias if one is present, otherwise the bare column name. Cast through
        # ``str`` because Star and similar expressions return weird values.
        name = getattr(select, "alias_or_name", "")
        return str(name) if name else ""

    @staticmethod
    def _alias_for_child_scope(child: Scope, parent: Scope) -> str | None:
        """Find the alias the parent scope uses for this child scope.

        ``Scope.sources`` is the canonical map qualifier -> source. The same
        Scope instance can appear under multiple aliases (a CTE referenced
        twice with different aliases), but each *instance* is bound to one
        introducing alias; we return the first match.
        """
        for alias, src in parent.sources.items():
            if src is child:
                return alias
        return None


def _synth_column_with_ref(ref: ColumnRef) -> exp.Column:
    """Build a synthetic ``exp.Column`` whose meta is pre-stamped with ``ref``.

    Used as the children of synthesised ``exp.Union`` nodes so the
    propagator's ``exp.Column`` branch recurses into each arm.
    """
    col = exp.Column(this=exp.to_identifier(ref.column))
    attach_column_ref(col, ref)
    return col


def _build_union_expression(arm_columns: list[exp.Column]) -> Expr:
    """Combine arm Columns into a left-associative chain of ``exp.Union`` nodes.

    sqlglot's ``exp.Union`` is binary (``this`` / ``expression``). For N arms
    we fold left so the propagator's ``exp.Union`` dispatch (plus-fold over
    children) visits every arm. The plus-fold is associative by the semiring
    laws, so the chain shape doesn't change the result.
    """
    if len(arm_columns) == 1:
        # A degenerate "union" with one arm is just that arm.
        return arm_columns[0]
    node: Expr = exp.Union(this=arm_columns[0], expression=arm_columns[1], distinct=False)
    for arm in arm_columns[2:]:
        node = exp.Union(this=node, expression=arm, distinct=False)
    return node


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
    """Schema dict for sqlglot's qualifier: ``{table_name: {column: type}}``.

    Lets sqlglot qualify columns cleanly. Type strings come from manifest
    column metadata; missing types default to ``UNKNOWN`` (a sqlglot-accepted
    placeholder).

    Tables with no documented columns are omitted from the result rather
    than emitted as ``{}``. The qualifier rejects an empty column dict,
    which would kill the build for any model that transitively touches an
    undocumented seed or source. When the table is simply absent from the
    schema dict, sqlglot trusts the qualifiers already present in the SQL
    and qualification proceeds.
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
