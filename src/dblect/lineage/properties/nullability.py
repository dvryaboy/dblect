"""Nullability property: per-column tri-state {NON_NULL, NULLABLE, UNKNOWN}.

The lattice orders by precision (NON_NULL refines NULLABLE refines UNKNOWN, the
"no information" top); ``meet`` keeps the stronger guarantee. A structural
property never contradicts, so the bottom (CONTRADICTION) is unreachable and
exists only to make the lattice bounded.

Confluence uses a semiring rather than the lattice join, so a proven NULLABLE
arm can beat an UNKNOWN one (a join with the top cannot); see
:class:`NullabilitySemiring` and ``propagation-soundness.md``.

Grounding comes from two discoverers that read a dbt manifest: a ``not_null``
generic test and a native ``NOT NULL`` constraint each ground a column to
NON_NULL. Both are sound-by-omission: a disabled test, a ``where``-conditional
test, or an axis they do not own grounds nothing rather than over-claiming. Build
the manifest-backed property with :func:`nullability_property`. The axis pieces a
custom grounding reuses (the lattice, the transfer catalogs, the semiring) are
public, so a graph-only test or a transfer demo can assemble its own property
without a manifest.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlglot import Expr
from sqlglot import expressions as exp

from dblect.adapters import AdapterProfile
from dblect.lineage.builder import build_manifest_graph, build_relation_graph
from dblect.lineage.facts.grounding import collect, grounding
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import (
    Annotation,
    Declared,
    DeclaredSource,
    Fact,
    NativeConstraint,
    Opacity,
    Predicate,
)
from dblect.lineage.facts.property import (
    AggregateRule,
    DepContext,
    FactDiscoverer,
    OperatorTransfer,
    Property,
    column_property,
    relation_property,
)
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceKind, SourceRef
from dblect.lineage.predicate import atoms_of, parse_predicate
from dblect.lineage.properties.predicate_flow import predicate_flow_property
from dblect.lineage.properties.uniqueness import (
    NO_KEYS,
    UNIQUENESS_LATTICE,
    CandidateKeySet,
    ConditionalKey,
    activate_conditional,
    relation_reduce,
)
from dblect.lineage.property import propagate
from dblect.manifest import ConstraintType, Manifest, ResourceType, generic_test_target_uid
from dblect.sql import SQLParseError, parse_sql
from dblect.sql import _sqlglot as sg
from dblect.sql._sqlglot import JoinSide


class Nullability(StrEnum):
    CONTRADICTION = "contradiction"  # formal lattice bottom; unreachable in propagation
    NON_NULL = "non_null"
    NULLABLE = "nullable"
    UNKNOWN = "unknown"


# Precision rank: smaller is more precise. CONTRADICTION < NON_NULL < NULLABLE < UNKNOWN.
_RANK: dict[Nullability, int] = {
    Nullability.CONTRADICTION: 0,
    Nullability.NON_NULL: 1,
    Nullability.NULLABLE: 2,
    Nullability.UNKNOWN: 3,
}


def _meet(a: Nullability, b: Nullability) -> Nullability:
    return a if _RANK[a] <= _RANK[b] else b


def _join(a: Nullability, b: Nullability) -> Nullability:
    return a if _RANK[a] >= _RANK[b] else b


NULLABILITY_LATTICE: Lattice[Nullability] = Lattice(
    meet=_meet,
    join=_join,
    top=Nullability.UNKNOWN,
    bottom=Nullability.CONTRADICTION,
)


@dataclass(frozen=True, slots=True)
class NullabilitySemiring:
    """The null-taint combine: ``plus`` (confluence) and ``times`` (scalar inputs)
    both take the more-null value, ordering NON_NULL < UNKNOWN < NULLABLE with
    NON_NULL as the identity. A proven NULLABLE taints the result whatever else is
    unknown, and UNKNOWN beats NON_NULL since we never claim non-null without
    evidence. CONTRADICTION never reaches the combine, so the laws are pinned over
    the three operational values in ``test_semiring_laws``."""

    zero: Nullability = Nullability.NON_NULL
    one: Nullability = Nullability.NON_NULL

    def plus(self, a: Nullability, b: Nullability) -> Nullability:
        if a is Nullability.NULLABLE or b is Nullability.NULLABLE:
            return Nullability.NULLABLE
        if a is Nullability.UNKNOWN or b is Nullability.UNKNOWN:
            return Nullability.UNKNOWN
        return Nullability.NON_NULL

    def times(self, a: Nullability, b: Nullability) -> Nullability:
        return self.plus(a, b)


def _coalesce_rule(
    _expr: Expr, kids: tuple[Annotation[Nullability], ...], _ctx: DepContext
) -> Annotation[Nullability]:
    """``COALESCE`` is non-null as soon as one argument is, whatever the rest are."""
    provisional = any(k.provisional for k in kids)
    values = [k.value for k in kids]
    if not values:
        return Annotation(Nullability.UNKNOWN, Opacity.IMPLICIT, provisional=provisional)
    if any(v is Nullability.NON_NULL for v in values):
        return Annotation(Nullability.NON_NULL, provisional=provisional)
    if all(v is Nullability.NULLABLE for v in values):
        return Annotation(Nullability.NULLABLE, provisional=provisional)
    return Annotation(Nullability.UNKNOWN, Opacity.IMPLICIT, provisional=provisional)


def _is_not_null_rule(
    _expr: Expr, kids: tuple[Annotation[Nullability], ...], _ctx: DepContext
) -> Annotation[Nullability]:
    """``x IS NOT NULL`` is a boolean that is itself never null."""
    provisional = any(k.provisional for k in kids)
    return Annotation(Nullability.NON_NULL, provisional=provisional)


def _nullif_rule(
    _expr: Expr, kids: tuple[Annotation[Nullability], ...], _ctx: DepContext
) -> Annotation[Nullability]:
    """``NULLIF(a, b)`` returns NULL when ``a = b``, so it admits a null by construction
    whatever its inputs are. This is a positive structural claim (the local twin of the
    outer-join optional side), not a fallback on uncertainty, so it grounds NULLABLE."""
    return Annotation(Nullability.NULLABLE, provisional=any(k.provisional for k in kids))


def _null_literal_rule(
    _expr: Expr, _kids: tuple[Annotation[Nullability], ...], _ctx: DepContext
) -> Annotation[Nullability]:
    """A bare ``NULL`` literal is null."""
    return Annotation(Nullability.NULLABLE)


class OuterJoinNull(exp.Expression):  # pyright: ignore[reportPrivateImportUsage]
    """A synthetic marker wrapping a column reference drawn from an outer join's optional
    side. The taint rewrite (:func:`taint_outer_joins`) inserts it into the nullability
    graph only; the rule below reads it to taint the value NULLABLE. Other properties
    never see it, since they run over the untainted graph. It subclasses ``Expression``
    (the concrete base) rather than ``Func`` so the standard node constructor works."""

    arg_types = {"this": True}  # noqa: RUF012 (sqlglot's arg-schema contract)


def _outer_join_null_rule(
    _expr: Expr, kids: tuple[Annotation[Nullability], ...], _ctx: DepContext
) -> Annotation[Nullability]:
    """An outer join pads its optional side with NULL on unmatched rows, so a column
    drawn from that side is nullable whatever its source nullability. This is a positive
    structural claim (the join's semantics admit a null here), not a fallback on
    uncertainty. A downstream guard (COALESCE, an IS NOT NULL filter) still clears it,
    because the guard's rule runs over this tainted child."""
    return Annotation(Nullability.NULLABLE, provisional=any(k.provisional for k in kids))


def _count_core(_expr: exp.AggFunc, child: Annotation[Nullability]) -> Annotation[Nullability]:
    """COUNT returns 0 for empty groups, never NULL."""
    return Annotation(Nullability.NON_NULL, provisional=child.provisional)


# The transfer catalogs are the reusable axis surface: :func:`nullability_property`
# and any custom-grounding caller (graph-only tests, transfer demos) build their
# property from these plus a ``ground`` function of their own.
NULLABILITY_OPERATORS: Mapping[type[Expr], OperatorTransfer[Nullability]] = {
    exp.Coalesce: _coalesce_rule,
    exp.Is: _is_not_null_rule,
    exp.Nullif: _nullif_rule,
    exp.Null: _null_literal_rule,
    OuterJoinNull: _outer_join_null_rule,
}
NULLABILITY_AGGREGATES: Mapping[type[exp.AggFunc], AggregateRule[Nullability]] = {
    exp.Count: AggregateRule(core=_count_core),
}


# --- discoverers -------------------------------------------------------------

_SOURCE_KIND: Mapping[ResourceType, SourceKind] = {
    ResourceType.MODEL: SourceKind.MODEL,
    ResourceType.SOURCE: SourceKind.SOURCE,
    ResourceType.SEED: SourceKind.SEED,
    ResourceType.SNAPSHOT: SourceKind.SNAPSHOT,
}


def _column_ref(manifest: Manifest, target_uid: str, column: str) -> ColumnRef | None:
    """The graph-keyed ColumnRef for ``column`` on the target node, or None if the
    node is absent or not a data-flow relation. Column names are case-folded to
    match how the builder keys the graph."""
    node = manifest.nodes.get(target_uid)
    if node is None:
        return None
    kind = _SOURCE_KIND.get(node.resource_type)
    if kind is None:
        return None
    return ColumnRef(SourceRef(kind, target_uid), column.lower())


class _NotNullTestDiscoverer:
    """Grounds NON_NULL from enabled ``not_null`` generic tests.

    A ``where`` filter makes the claim conditional: the fact carries the predicate
    and is captured, but grounding does not fold a conditional NON_NULL into the
    unconditional annotation (see :class:`~dblect.lineage.facts.model.Predicate`)."""

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[Nullability, ColumnRef]]:
        out: list[Fact[Nullability, ColumnRef]] = []
        for node in manifest.nodes.values():
            tm = node.test_metadata
            if tm is None or not tm.enabled or tm.name != "not_null":
                continue
            col = tm.kwargs.get("column_name")
            if not isinstance(col, str) or not col:
                continue
            target = generic_test_target_uid(node)
            if target is None:
                continue
            scope = _column_ref(manifest, target, col)
            if scope is None:
                continue
            out.append(
                Fact(
                    scope=scope,
                    value=Nullability.NON_NULL,
                    provenance=Declared(DeclaredSource.DBT_GENERIC_TEST),
                    detail=node.name,
                    condition=Predicate(tm.where) if tm.where is not None else None,
                )
            )
        return out


class _NativeNotNullDiscoverer:
    """Grounds NON_NULL from native ``NOT NULL`` constraints (dbt 1.5+).

    Whether the constraint is enforced on write is the adapter profile's call
    (NOT NULL is enforced on essentially every warehouse); the flag is descriptive
    provenance, read only by the unenforced-constraint finding."""

    def __init__(self, profile: AdapterProfile) -> None:
        self._enforced = profile.not_null_enforced

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[Nullability, ColumnRef]]:
        out: list[Fact[Nullability, ColumnRef]] = []
        for node in manifest.nodes.values():
            if node.resource_type is not ResourceType.MODEL:
                continue
            source = SourceRef(SourceKind.MODEL, node.unique_id)
            # Model-level constraints name their columns explicitly.
            out.extend(
                self._fact(source, col, "model-level NOT NULL")
                for c in node.constraints
                if c.type is ConstraintType.NOT_NULL
                for col in c.columns
            )
            # Column-level constraints attach to the column implicitly.
            out.extend(
                self._fact(source, col_name, f"column-level NOT NULL on {col_name}")
                for col_name, col in node.columns.items()
                for c in col.constraints
                if c.type is ConstraintType.NOT_NULL
            )
        return out

    def _fact(self, source: SourceRef, column: str, detail: str) -> Fact[Nullability, ColumnRef]:
        return Fact(
            scope=ColumnRef(source, column.lower()),
            value=Nullability.NON_NULL,
            provenance=NativeConstraint(enforced_on_write=self._enforced),
            detail=detail,
        )


def not_null_test_discoverer() -> FactDiscoverer[Nullability, ColumnRef]:
    return _NotNullTestDiscoverer()


def native_not_null_discoverer(profile: AdapterProfile) -> FactDiscoverer[Nullability, ColumnRef]:
    return _NativeNotNullDiscoverer(profile)


def nullability_property(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    name_to_source: Mapping[str, SourceRef],
    extra: tuple[FactDiscoverer[Nullability, ColumnRef], ...] = (),
) -> Property[Nullability, ColumnRef]:
    """The manifest-backed nullability property: grounding folds the discoverers'
    NON_NULL claims (plus any ``extra``) through the lattice, leaving every
    undeclared column UNKNOWN. No opaque opt-out reader is wired yet, so the
    opaque set is empty. ``profile`` is the run's resolved target, fixing the
    adapter's enforcement semantics."""
    discoverers = (
        not_null_test_discoverer(),
        native_not_null_discoverer(profile),
        *extra,
    )
    facts = collect(manifest, discoverers, name_to_source=name_to_source)
    return column_property(
        name="nullability",
        lattice=NULLABILITY_LATTICE,
        operators=NULLABILITY_OPERATORS,
        aggregates=NULLABILITY_AGGREGATES,
        ground=grounding(facts, opaque=set(), lat=NULLABILITY_LATTICE),
        semiring=NullabilitySemiring(),
    )


# --- conditional activation --------------------------------------------------
#
# A ``where``-filtered ``not_null`` activates the same way a conditional key does: at
# a scope whose row filter implies the predicate. Nullability is column-scoped, but
# the carrying and predicate-renaming a conditional claim needs are relation-scoped,
# so we reuse the uniqueness carrier: a conditional NON_NULL column is a one-column
# conditional "key" (the column is non-null under the predicate), flowed across
# relations by ``relation_reduce`` and promoted by ``activate_conditional``. The
# activated columns then fold NON_NULL into the column annotations.


def _conditional_notnull_carrier(
    manifest: Manifest, profile: AdapterProfile
) -> Property[CandidateKeySet, SourceRef]:
    """A relation-scoped carrier for conditional NON_NULL columns, grounded from the
    ``where``-filtered ``not_null`` tests and flowed across model boundaries.

    The value type is uniqueness's :class:`CandidateKeySet` because the relation-algebra
    carrying it needs (rename columns and predicates through each projection, drop on a
    join / group / computed projection) is property-agnostic; only the *meaning* of the
    payload differs, and a one-column key reads here as "this column is non-null under
    the predicate". The borrow is the pragmatic reuse for the second axis. If a third
    axis wants the same carrying, lift this into a generic
    ``conditional_carrier(claims, lattice)`` rather than reaching further into
    uniqueness, so the shared mechanism stops depending on one property's value type.
    """
    facts = collect(
        manifest,
        (not_null_test_discoverer(), native_not_null_discoverer(profile)),
        name_to_source={},
    )
    conditional = _conditional_columns_by_relation(facts)

    def ground(scope: SourceRef) -> Annotation[CandidateKeySet]:
        cks = conditional.get(scope)
        if cks is None:
            return Annotation(NO_KEYS, Opacity.IMPLICIT)
        # CONCRETE so reconcile keeps the conditional payload (an IMPLICIT grounded
        # value is dropped in favour of the inferred one).
        return Annotation(CandidateKeySet(frozenset(), cks), Opacity.CONCRETE)

    return relation_property(
        name="conditional_not_null",
        lattice=UNIQUENESS_LATTICE,
        operators={},
        aggregates={},
        ground=ground,
        reconcile_by_meet=True,
        reducer=relation_reduce,
    )


def _conditional_columns_by_relation(
    facts: Mapping[ColumnRef, tuple[Fact[Nullability, ColumnRef], ...]],
) -> dict[SourceRef, frozenset[ConditionalKey]]:
    """Group ``where``-filtered NON_NULL facts into one-column conditional keys per
    relation, parsing each predicate to atoms. A predicate that does not parse carries
    no information, so its column is dropped rather than activated on a guess."""
    out: dict[SourceRef, set[ConditionalKey]] = {}
    for bucket in facts.values():
        for fact in bucket:
            if fact.condition is None:
                continue
            parsed = parse_predicate(fact.condition.sql)
            if parsed is None:
                continue
            claim = ConditionalKey(frozenset({fact.scope.column}), atoms_of(parsed))
            out.setdefault(fact.scope.source, set()).add(claim)
    return {relation: frozenset(claims) for relation, claims in out.items()}


# --- outer-join taint --------------------------------------------------------
#
# An outer join pads its optional side with NULL on unmatched rows, so a column drawn
# from that side is nullable downstream even when it is NON_NULL at its own source. The
# taint is a fact about the join, not the column expression, so it cannot be grounded
# (a more precise NON_NULL inference would win the reconcile) and has to enter
# inference. We do that by rewriting the nullability graph: each optional-side column
# reference is wrapped in an :class:`OuterJoinNull` marker whose rule taints NULLABLE.
# Because the marker sits in the expression the propagator walks, the taint rides
# downstream through every consumer and a guard (COALESCE, IS NOT NULL) still clears it.


def _relation_alias(e: Expr) -> str | None:
    """The qualifier a FROM/JOIN source lends its columns downstream, case-folded to match
    the graph, or ``None`` when it lends none. A table contributes its alias-or-name; an
    aliased derived table (subquery) contributes its alias. Both are the qualifier the
    builder stamps onto columns drawn from the source, so the taint can find them. Anything
    else (an unaliased subquery, ``UNNEST``, a lateral) yields ``None`` and stays untainted:
    a sound under-approximation, and lateral/unnest row-drop is a separate property's axis."""
    if isinstance(e, (exp.Table, exp.Subquery)):
        name = sg.name_of(e)
        return name.lower() if name else None
    return None


def _optional_join_aliases(select: exp.Select) -> set[str]:
    """The FROM-clause aliases whose columns an outer join can pad with NULL: the
    joined-in side of a LEFT join, the accumulated left side of a RIGHT join, both for a
    FULL join. INNER and CROSS pad nothing. Aliases are case-folded to match the graph.

    This is the side-blind reading of :func:`_optional_alias_sides`: an alias is optional
    exactly when that function attributes it a padding join, so the set is its keyset."""
    return set(_optional_alias_sides(select))


def _wrap_optional_columns(expr: Expr, optional: set[str]) -> Expr:
    """A copy of ``expr`` with every column qualified by an optional-side alias wrapped in
    :class:`OuterJoinNull`. Unqualified columns are left alone (the side is unknown), the
    silent-when-unsure posture that keeps the taint from over-firing."""
    rewritten = expr.copy()
    targets = [c for c in rewritten.find_all(exp.Column) if c.table and c.table.lower() in optional]
    for col in targets:
        col.replace(OuterJoinNull(this=col.copy()))
    return rewritten


def taint_outer_joins(
    graph: ColumnLineageGraph,
    manifest: Manifest,
    *,
    parsed: Mapping[str, Expr] | None = None,
) -> ColumnLineageGraph:
    """A copy of ``graph`` whose model output expressions taint their outer-join
    optional-side column references. Each model's top-level FROM/JOIN structure decides
    which aliases are optional; CTE-collapsed or unparseable models are left untouched
    (the taint then simply does not fire, a sound under-approximation). ``parsed`` shares
    the audit's already-parsed trees so the join analysis re-parses nothing."""
    refs_by_source: dict[SourceRef, list[ColumnRef]] = {}
    for ref in graph.expressions:
        refs_by_source.setdefault(ref.source, []).append(ref)
    new_expressions = dict(graph.expressions)
    for node in manifest.nodes.values():
        if node.resource_type is not ResourceType.MODEL:
            continue
        sql = node.compiled_code
        if not sql:
            continue
        tree = parsed.get(node.unique_id) if parsed is not None else None
        if tree is None:
            try:
                tree = parse_sql(sql, dialect="duckdb")
            except SQLParseError:
                continue
        select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
        if not isinstance(select, exp.Select):
            continue
        optional = _optional_join_aliases(select)
        if not optional:
            continue
        model_ref = SourceRef(SourceKind.MODEL, node.unique_id)
        for ref in refs_by_source.get(model_ref, ()):
            new_expressions[ref] = _wrap_optional_columns(graph.expressions[ref], optional)
    return ColumnLineageGraph(edges=graph.edges, expressions=new_expressions)


def _optional_alias_sides(select: exp.Select) -> dict[str, JoinSide]:
    """Each optional-side alias mapped to the join kind that makes it optional.

    The same reading as :func:`_optional_join_aliases`, retaining the side so a consumer can
    name *which* outer join padded the column. A LEFT join's joined-in side reports LEFT, a
    RIGHT join's accumulated left side reports RIGHT, a FULL join reports FULL for both. When
    an alias becomes optional through more than one join, the last to taint it wins; the side
    is descriptive, the optionality itself is what the taint relies on."""
    from_ = sg.from_of(select)
    left: set[str] = set()
    if from_ is not None:
        base = _relation_alias(from_.this)
        if base is not None:
            left.add(base)
    optional: dict[str, JoinSide] = {}
    for join in sg.joins_of(select):
        side = sg.join_side_of(join)
        alias = _relation_alias(join.this)
        if side is JoinSide.LEFT and alias is not None:
            optional[alias] = JoinSide.LEFT
        elif side is JoinSide.RIGHT:
            for a in left:
                optional[a] = JoinSide.RIGHT
        elif side is JoinSide.FULL:
            if alias is not None:
                optional[alias] = JoinSide.FULL
            for a in left:
                optional[a] = JoinSide.FULL
        if alias is not None:
            left.add(alias)
    return optional


def _outer_join_output_columns(
    select: exp.Select, optional: Mapping[str, JoinSide]
) -> dict[str, JoinSide]:
    """The model's *output* columns produced from an outer-join optional side, each mapped
    to the join kind that made it nullable.

    A top-level projection contributes its output name when its expression is a bare column
    qualified by an optional-side alias. ``COALESCE`` and other guards clear the optionality,
    so a projection that wraps the column reports no outer-join cause. A projection whose
    source we cannot read as a bare optional-side column reports nothing (the
    silent-when-unsure posture), so the cause is a sufficient, never speculative, condition.
    Output names are case-folded to match the detectors' lowercased keys."""
    out: dict[str, JoinSide] = {}
    for proj in select.selects:
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if not isinstance(inner, exp.Column):
            continue
        side = optional.get(inner.table.lower()) if inner.table else None
        if side is not None:
            out[sg.name_of(proj).lower()] = side
    return out


def outer_join_nullable_columns(
    manifest: Manifest, *, parsed: Mapping[str, Expr] | None = None
) -> Mapping[str, Mapping[str, JoinSide]]:
    """Per model name, the output columns whose nullability the substrate attributes to an
    outer join at that model's own top-level FROM/JOIN structure, each mapped to the join
    kind (LEFT/RIGHT/FULL) that padded it.

    This is the same structural reading :func:`taint_outer_joins` uses, surfaced so a
    consumer (the join-on-nullable-key finding) can name *why* a key is nullable upstream
    without rediscovering it. It is a sufficient condition: a model the analysis cannot
    read (CTE-collapsed, unparseable, a projection that is not a bare optional-side column)
    contributes nothing rather than a guess. Keyed by ``identifier or name`` to match how
    the detectors resolve a relation in compiled SQL."""
    out: dict[str, Mapping[str, JoinSide]] = {}
    for node in manifest.nodes.values():
        if node.resource_type is not ResourceType.MODEL:
            continue
        sql = node.compiled_code
        if not sql:
            continue
        tree = parsed.get(node.unique_id) if parsed is not None else None
        if tree is None:
            try:
                tree = parse_sql(sql, dialect="duckdb")
            except SQLParseError:
                continue
        select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
        if not isinstance(select, exp.Select):
            continue
        optional = _optional_alias_sides(select)
        if not optional:
            continue
        columns = _outer_join_output_columns(select, optional)
        if columns:
            out[node.identifier or node.name] = columns
    return out


def activated_nullability(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
    column_graph: ColumnLineageGraph | None = None,
) -> Mapping[ColumnRef, Annotation[Nullability]]:
    """Per-column nullability with outer-join optional sides tainted NULLABLE and
    conditional NON_NULL facts activated against the predicate flow.

    The base annotations come from the column-scoped property over the outer-join-tainted
    graph, so an optional-side column reads NULLABLE even when its source is NON_NULL. On
    top of that, the conditional carrier flows each ``where``-filtered NON_NULL across
    relations, the flow says which scopes satisfy the predicate, and every activated
    column folds NON_NULL into its annotation. ``parsed`` shares the audit's already-parsed
    trees so the whole pass re-parses nothing; the nullability detectors are its consumer.
    ``column_graph`` lets the audit pass the manifest column graph it built once, so the
    qualify-and-resolve walk is not repeated per fact family. ``profile`` is the run's
    resolved target, fixing both the parse dialect and the enforcement semantics so they
    agree.
    """
    dialect = profile.sqlglot_dialect
    base_graph = (
        column_graph
        if column_graph is not None
        else build_manifest_graph(manifest, dialect=dialect, parsed=parsed).graph
    )
    tainted = taint_outer_joins(base_graph, manifest, parsed=parsed)
    base = dict(propagate(tainted, nullability_property(manifest, profile, name_to_source={})))
    relation_graph = build_relation_graph(manifest, dialect=dialect, parsed=parsed).graph
    carrier = propagate(relation_graph, _conditional_notnull_carrier(manifest, profile))
    # Flow is consulted only where a conditional claim waits to activate, so seed the
    # flow pass with those scopes and let it pull in their upstreams rather than walking
    # every relation. ``activate_conditional`` reads flow only at carrier scopes whose
    # value carries a conditional payload, so a scope left out of the seed activates
    # nothing it would have otherwise (the same seeding invariant the uniqueness
    # detector relies on).
    conditional_scopes = [ref for ref, ann in carrier.items() if ann.value.conditional]
    flow = propagate(relation_graph, predicate_flow_property(), subjects=conditional_scopes)
    for ref, activated in activate_conditional(carrier, flow).items():
        for key in activated.keys:
            for column in key:
                column_ref = ColumnRef(ref, column)
                prior = base.get(column_ref)
                if prior is None:
                    base[column_ref] = Annotation(Nullability.NON_NULL, Opacity.CONCRETE)
                else:
                    base[column_ref] = Annotation(
                        _meet(prior.value, Nullability.NON_NULL), prior.opacity, prior.provisional
                    )
    return base
