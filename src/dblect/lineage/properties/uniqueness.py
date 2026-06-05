"""Uniqueness property: a relation's set of candidate keys.

A relation's value is the set of candidate keys it is known to be unique on,
each key a set of column names. In the K-relations framing this is
``K = P(P(column))`` (Green, Karvounarakis, Tannen 2007); uniqueness is the first
relation-scoped property, so its scope is a :class:`SourceRef`, not a column.

The lattice orders by precision, where knowing *more* keys is more precise:
``x`` refines ``y`` when ``x``'s key set is a superset of ``y``'s. ``meet``
(resolution of several declarations at one relation) unions the key sets, so two
``unique`` declarations simply both hold; ``join`` (confluence at a ``UNION``)
keeps only the keys both branches carry. ``top`` is the empty key set ("we know
of no key"), the value every undeclared relation grounds to. ``bottom`` is a
formal universal element that makes the lattice bounded; uniqueness never
contradicts (two key declarations always union cleanly), so it is unreachable in
resolution and exists only so ``meet`` / ``join`` have their annihilator and
identity.

Confluence and the cross at a ``JOIN`` are not a plain semiring: the ``JOIN``
combine reads which columns the ON predicate equates, so it is an operator rule
over :class:`~sqlglot.expressions.Join` rather than a value-only ``times``. The
transfer catalogs and the relation walk land with the propagator's
relation-scoped dispatch; this module defines the value type, its lattice, and
the discoverers that ground it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import cast

import sqlglot.expressions as exp
from sqlglot import Expr

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
from dblect.lineage.facts.property import DepContext, FactDiscoverer, Property, relation_property
from dblect.lineage.graph import SourceKind, SourceRef, source_ref_meta
from dblect.lineage.predicate import (
    Canon,
    CmpAtom,
    InAtom,
    atom_column,
    atoms_of,
    parse_predicate,
    rename_atom,
)
from dblect.lineage.properties.activation import activate
from dblect.lineage.properties.predicate_flow import RowFilter
from dblect.manifest import (
    ConstraintSpec,
    ConstraintType,
    Manifest,
    ResourceType,
    generic_test_target_uid,
)
from dblect.sql import _sqlglot as sg
from dblect.sql._sqlglot import JoinSide

# A single candidate key is a set of (case-folded) column names; a relation can
# carry several, so its value is a set of those.
Key = frozenset[str]


@dataclass(frozen=True, slots=True)
class ConditionalKey:
    """A candidate key that holds only over the rows matching ``predicate``.

    A ``where``-filtered ``unique`` test grounds one of these rather than an
    unconditional key. It is carried (never folded into ``keys``) until a scope's
    flowed row filter implies ``predicate``, at which point activation promotes it.
    ``predicate`` is the test's ``where`` parsed to the engine's atoms, so it feeds
    :func:`~dblect.lineage.predicate.entails_atoms` directly.
    """

    key: Key
    predicate: frozenset[Canon]


@dataclass(frozen=True, slots=True)
class CandidateKeySet:
    """The set of candidate keys a relation is known to be unique on.

    ``keys`` holds the unconditionally known keys; the empty set is the lattice
    ``top`` ("no key known"). ``conditional`` carries keys that hold only over a row
    filter, captured for activation and never counted among ``keys`` until promoted.
    ``is_bottom`` marks the formal universal element (the lattice ``bottom``): it
    absorbs under ``meet`` and is the identity under ``join``, and no resolution of
    real declarations reaches it, since uniqueness claims only ever union. Equality
    is structural, so ``CandidateKeySet(frozenset())`` (top) and the bottom sentinel
    are distinct values.
    """

    keys: frozenset[Key]
    conditional: frozenset[ConditionalKey] = frozenset()
    is_bottom: bool = False

    @staticmethod
    def of(*keys: frozenset[str]) -> CandidateKeySet:
        """A key set from explicit keys, each already a set of column names."""
        return CandidateKeySet(frozenset(keys))


# The empty key set: "we know of no candidate key", the value every undeclared
# relation grounds to and the meet identity.
NO_KEYS: CandidateKeySet = CandidateKeySet(frozenset())

# The formal universal element. Unreachable when resolving real declarations
# (they only union), present so the lattice is bounded.
ALL_KEYS: CandidateKeySet = CandidateKeySet(frozenset(), is_bottom=True)


def _meet(a: CandidateKeySet, b: CandidateKeySet) -> CandidateKeySet:
    """Most precise value consistent with both: union of the known keys (and of the
    carried conditional keys, which also only ever accumulate).

    Bottom annihilates (it already "knows" every key), so a meet touching bottom
    stays bottom.
    """
    if a.is_bottom or b.is_bottom:
        return ALL_KEYS
    return CandidateKeySet(a.keys | b.keys, a.conditional | b.conditional)


def _join(a: CandidateKeySet, b: CandidateKeySet) -> CandidateKeySet:
    """Least precise value both refine: the keys both sides carry (intersection),
    conditional keys likewise.

    Bottom is the identity (it refines nothing finer than the other side), so a
    join with bottom returns the other operand.
    """
    if a.is_bottom:
        return b
    if b.is_bottom:
        return a
    return CandidateKeySet(a.keys & b.keys, a.conditional & b.conditional)


UNIQUENESS_LATTICE: Lattice[CandidateKeySet] = Lattice(
    meet=_meet,
    join=_join,
    top=NO_KEYS,
    bottom=ALL_KEYS,
)


# --- discoverers -------------------------------------------------------------

# Uniqueness grounds on relations a downstream model can ref by name: models and
# sources. Seeds and snapshots are eligible in the shared default but kept out
# until their downstream consumers are tested against them; see issue #52.
_TARGET_PREFIXES: tuple[str, ...] = ("model.", "source.")

_SOURCE_KIND: Mapping[ResourceType, SourceKind] = {
    ResourceType.MODEL: SourceKind.MODEL,
    ResourceType.SOURCE: SourceKind.SOURCE,
    ResourceType.SEED: SourceKind.SEED,
    ResourceType.SNAPSHOT: SourceKind.SNAPSHOT,
}

_KEY_CONSTRAINT_TYPES: frozenset[ConstraintType] = frozenset(
    {ConstraintType.PRIMARY_KEY, ConstraintType.UNIQUE}
)

# Whether the active adapter enforces PRIMARY KEY / UNIQUE on write. Most cloud
# warehouses treat them as advisory (Snowflake, BigQuery, Redshift enforce
# neither), so the default is unenforced; the set names adapters that do enforce.
# The flag is descriptive provenance, read only by the unenforced-constraint
# finding, never by fact resolution.
_KEY_ENFORCING_ADAPTERS: frozenset[str] = frozenset({"duckdb", "postgres"})


def _key_enforced(adapter_type: str) -> bool:
    return adapter_type.lower() in _KEY_ENFORCING_ADAPTERS


def _source_ref(manifest: Manifest, target_uid: str) -> SourceRef | None:
    """The graph-keyed ``SourceRef`` for a target node, or ``None`` if the node is
    absent or not a relation uniqueness can address."""
    node = manifest.nodes.get(target_uid)
    if node is None:
        return None
    kind = _SOURCE_KIND.get(node.resource_type)
    return SourceRef(kind, target_uid) if kind is not None else None


def _single_key(*cols: str) -> CandidateKeySet:
    """A one-key value naming a case-folded candidate key."""
    return CandidateKeySet.of(frozenset(c.lower() for c in cols))


class _UniqueTestDiscoverer:
    """Grounds a single-column key from an enabled ``unique`` test.

    A ``where`` filter makes the key conditional: the fact carries the predicate
    and is captured, but grounding does not fold it into the unconditional key set
    (see :class:`~dblect.lineage.facts.model.Predicate`)."""

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[CandidateKeySet, SourceRef]]:
        out: list[Fact[CandidateKeySet, SourceRef]] = []
        for node in manifest.nodes.values():
            tm = node.test_metadata
            if tm is None or not tm.enabled or tm.name != "unique":
                continue
            col = tm.kwargs.get("column_name")
            if not isinstance(col, str) or not col:
                continue
            target = generic_test_target_uid(node, eligible_prefixes=_TARGET_PREFIXES)
            scope = _source_ref(manifest, target) if target is not None else None
            if scope is None:
                continue
            out.append(
                Fact(
                    scope=scope,
                    value=_single_key(col),
                    provenance=Declared(DeclaredSource.DBT_GENERIC_TEST),
                    detail=node.name,
                    condition=Predicate(tm.where) if tm.where is not None else None,
                )
            )
        return out


class _UniqueCombinationDiscoverer:
    """Grounds a composite key from a ``unique_combination_of_columns`` test."""

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[CandidateKeySet, SourceRef]]:
        out: list[Fact[CandidateKeySet, SourceRef]] = []
        for node in manifest.nodes.values():
            tm = node.test_metadata
            if tm is None or not tm.enabled:
                continue
            # dbt-utils tests carry the package namespace; match the bare name so
            # an aliased install (``my_utils.unique_combination_of_columns``) still
            # grounds.
            if not tm.name.endswith("unique_combination_of_columns"):
                continue
            raw = tm.kwargs.get("combination_of_columns")
            if not isinstance(raw, list):
                continue
            raw_list = cast("list[object]", raw)
            cols = [c for c in raw_list if isinstance(c, str) and c]
            # Every entry must be a usable column name; a partially-typed list
            # (a nested list, a null) is a shape we can't ground, so skip it
            # rather than ground a partial key.
            if not cols or len(cols) != len(raw_list):
                continue
            target = generic_test_target_uid(node, eligible_prefixes=_TARGET_PREFIXES)
            scope = _source_ref(manifest, target) if target is not None else None
            if scope is None:
                continue
            out.append(
                Fact(
                    scope=scope,
                    value=_single_key(*cols),
                    provenance=Declared(DeclaredSource.DBT_UTILS_TEST),
                    detail=node.name,
                    condition=Predicate(tm.where) if tm.where is not None else None,
                )
            )
        return out


class _NativeKeyDiscoverer:
    """Grounds keys from native ``PRIMARY KEY`` / ``UNIQUE`` constraints (dbt 1.5+).

    Model-level constraints name their columns explicitly; a column-level
    constraint is the implicit single-column key on the column it attaches to.
    """

    def __init__(self, adapter_type: str) -> None:
        self._enforced = _key_enforced(adapter_type)

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[CandidateKeySet, SourceRef]]:
        out: list[Fact[CandidateKeySet, SourceRef]] = []
        for node in manifest.nodes.values():
            if node.resource_type is not ResourceType.MODEL:
                continue
            source = SourceRef(SourceKind.MODEL, node.unique_id)
            # Model-level constraints name their columns explicitly.
            out.extend(
                self._fact(source, cols, f"model-level {c.type.value}")
                for c in node.constraints
                if (cols := _key_columns(c)) is not None
            )
            # Column-level constraints attach to the column implicitly.
            out.extend(
                self._fact(source, (col_name,), f"column-level {c.type.value} on {col_name}")
                for col_name, col in node.columns.items()
                for c in col.constraints
                if c.type in _KEY_CONSTRAINT_TYPES
            )
        return out

    def _fact(
        self, source: SourceRef, cols: tuple[str, ...], detail: str
    ) -> Fact[CandidateKeySet, SourceRef]:
        return Fact(
            scope=source,
            value=_single_key(*cols),
            provenance=NativeConstraint(enforced_on_write=self._enforced),
            detail=detail,
        )


def _key_columns(c: ConstraintSpec) -> tuple[str, ...] | None:
    """The columns a model-level key constraint names, or ``None`` if ``c`` is not
    a key constraint or names no columns."""
    if c.type not in _KEY_CONSTRAINT_TYPES or not c.columns:
        return None
    return tuple(c.columns)


def unique_test_discoverer() -> FactDiscoverer[CandidateKeySet, SourceRef]:
    return _UniqueTestDiscoverer()


def unique_combination_discoverer() -> FactDiscoverer[CandidateKeySet, SourceRef]:
    return _UniqueCombinationDiscoverer()


def native_key_discoverer(adapter_type: str) -> FactDiscoverer[CandidateKeySet, SourceRef]:
    return _NativeKeyDiscoverer(adapter_type)


# --- the relation reducer ----------------------------------------------------
#
# The relation-algebra walk for candidate keys. It mirrors the column reducer's
# job (turn a derivation into an inferred annotation, recursing into referenced
# nodes) but over relation algebra: a FROM carries the source's keys, a JOIN keeps
# the probe side's keys only when the joined-in side is unique on the join
# columns, GROUP BY / DISTINCT introduce a key, UNION ALL keeps none, and the
# projection remaps keys onto output names. Posture is silent-when-unproven: a
# shape the walk does not model drops keys rather than over-claiming.

# A column qualified by its FROM/JOIN source alias, tracked inside one scope so a
# multi-source scope's join keys line up; the qualifier collapses to bare output
# names at the projection boundary.
_QCol = tuple[str, str]
_QKey = frozenset[_QCol]


@dataclass(frozen=True, slots=True)
class _Carried:
    """What a scope carries up the walk: its candidate keys and the conditional keys
    riding through it (each in the scope's own output column names)."""

    keys: frozenset[Key]
    conditional: frozenset[ConditionalKey] = frozenset()


_EMPTY: _Carried = _Carried(frozenset())

# Resolves a base (non-CTE) table reference to what it carries. Two implementations:
# the graph reducer reads the table's stamped SourceRef and recurses through the
# shared propagator (so conditional keys cross model boundaries); the detector index
# resolves the table by name against the per-model keys propagation already produced
# (it consumes already-activated keys, so it carries no conditional payload).
_BaseResolve = Callable[["exp.Table"], _Carried]


def relation_reduce(
    deriv: Expr,
    prop: Property[CandidateKeySet, SourceRef],
    recurse: Callable[[SourceRef], Annotation[CandidateKeySet]],
    _ctx: DepContext,
    _default: Annotation[CandidateKeySet],
) -> Annotation[CandidateKeySet]:
    """Reduce a model's relational tree to its inferred candidate-key set.

    A base table resolves through ``recurse`` on its stamped ``SourceRef``, so
    cross-model keys, conditional keys, declarations, and the provisional taint flow
    in. CTEs and inline subqueries are resolved structurally within the walk.

    This also serves as the relation-algebra carrier for any one-column conditional
    claim, since a relation that grounds no unconditional keys carries only its
    conditional payload: nullability reuses it to flow conditional NON_NULL columns
    (a one-column :class:`ConditionalKey` per column) across model boundaries.
    """
    provisional = False

    def base_resolve(table: exp.Table) -> _Carried:
        nonlocal provisional
        ref = source_ref_meta(table)
        if ref is None:
            return _EMPTY
        ann = recurse(ref)
        provisional = provisional or ann.provisional
        return _Carried(ann.value.keys, ann.value.conditional)

    carried = _RelationWalk(base_resolve).scope_keys(deriv, cte_scope={})
    value = CandidateKeySet(carried.keys, carried.conditional)
    opacity = Opacity.CONCRETE if (carried.keys or carried.conditional) else Opacity.IMPLICIT
    return Annotation(value, opacity, provisional=provisional)


def relation_scope_keys(
    tree: Expr, model_keys: Mapping[str, frozenset[Key]]
) -> Mapping[int, frozenset[Key]]:
    """Per-scope candidate keys for every SELECT/UNION node in ``tree``, keyed by
    ``id(node)``.

    The same relation algebra the reducer runs, but for one already-parsed tree
    and with base tables resolved by name against ``model_keys`` (the per-model
    keys propagation produced) rather than by stamp. This is what an audit
    detector consults to get a CTE's or inline subquery's keys, since those
    intermediate scopes are not relations the propagator annotates. The returned
    map is valid only for the lifetime of ``tree``.

    Base keys here are already activated (the per-model map is built after
    activation), so this walk carries no conditional payload of its own.
    """

    def base_resolve(table: exp.Table) -> _Carried:
        return _Carried(model_keys.get(table.name, frozenset()))

    walk = _RelationWalk(base_resolve, record=True)
    walk.scope_keys(tree, cte_scope={})
    return {node_id: carried.keys for node_id, carried in walk.scopes.items()}


def activated_scope_keys(
    tree: Expr,
    model_keys: Mapping[str, frozenset[Key]],
    conditional_by_name: Mapping[str, frozenset[ConditionalKey]],
    scope_flow: Mapping[int, frozenset[Canon]],
) -> Mapping[int, frozenset[Key]]:
    """Per-scope candidate keys with conditional keys activated against each scope's
    own row filter, keyed by ``id(node)``.

    Like :func:`relation_scope_keys`, but base tables resolve to both their keys and
    their conditional keys, so the walk carries conditional keys into each
    intermediate scope, and each scope promotes the ones its flow (``scope_flow``,
    from :func:`~dblect.lineage.properties.predicate_flow.relation_scope_filters`)
    implies. This lets a window or join over a CTE that filters an upstream see the
    key the filter activates.
    """

    def base_resolve(table: exp.Table) -> _Carried:
        name = table.name
        return _Carried(
            model_keys.get(name, frozenset()), conditional_by_name.get(name, frozenset())
        )

    walk = _RelationWalk(base_resolve, record=True)
    walk.scope_keys(tree, cte_scope={})
    out: dict[int, frozenset[Key]] = {}
    for node_id, carried in walk.scopes.items():
        # The flow walk does not record every scope this key walk does: it stops at a
        # join rather than recursing into it, so a scope nested inside a joined
        # subquery has no recorded flow. ``scope_flow.get`` defaults such a scope to
        # the empty filter, which implies nothing and so activates nothing. That is
        # the safe direction (a conditional key stays conditional), and it matches the
        # flow's own posture of dropping at a join.
        promoted = activate(
            CandidateKeySet(carried.keys),
            ((CandidateKeySet.of(ck.key), ck.predicate) for ck in carried.conditional),
            scope_flow.get(node_id, frozenset()),
            _meet,
        )
        out[node_id] = promoted.keys
    return out


class _RelationWalk:
    """Bottom-up candidate-key inference over one relational tree.

    ``base_resolve`` resolves a base (non-CTE) table to what it carries; CTEs and
    inline subqueries are resolved structurally within the walk. Conditional keys
    ride through, renamed onto a scope's output columns, wherever the row set is not
    multiplied and their columns stay disambiguated: a single source, or a
    non-multiplying join under a star-free projection. A GROUP BY, a UNION, a
    fanning-out join, or a star over a join drops them, so a carried key only ever
    survives where it could still soundly activate downstream. With ``record`` set,
    every SELECT/UNION scope's output is kept in ``scopes`` keyed by ``id(node)`` so
    a detector can read intermediate-scope keys.
    """

    def __init__(self, base_resolve: _BaseResolve, *, record: bool = False) -> None:
        self._base_resolve = base_resolve
        self._record = record
        self.scopes: dict[int, _Carried] = {}

    def scope_keys(self, node: Expr, *, cte_scope: Mapping[str, _Carried]) -> _Carried:
        if isinstance(node, exp.Select):
            carried = self._select(node, cte_scope=cte_scope)
        elif isinstance(node, exp.Union):
            carried = self._union(node, cte_scope=cte_scope)
        else:
            return _EMPTY
        if self._record:
            self.scopes[id(node)] = carried
        return carried

    def _select(self, sel: exp.Select, *, cte_scope: Mapping[str, _Carried]) -> _Carried:
        local = dict(cte_scope)
        with_ = sel.args.get("with_")
        if isinstance(with_, exp.With):
            for cte in with_.expressions:
                if isinstance(cte, exp.CTE) and isinstance(cte.this, Expr):
                    local[cte.alias_or_name] = self.scope_keys(cte.this, cte_scope=local)

        from_ = sg.from_of(sel)
        if from_ is None or not isinstance(from_.this, Expr):
            return _EMPTY
        resolved = self._resolve_source(from_.this, cte_scope=local)
        if resolved is None:
            return _EMPTY
        from_alias, from_carried = resolved
        combined = _qualify(from_alias, from_carried.keys)

        # WHERE filters cannot add duplicates, so keys (and conditional keys) are
        # preserved across it; the WHERE is the predicate flow's concern, not ours.
        joins = sg.joins_of(sel)
        joins_preserve = True
        for j in joins:
            preserved = self._join_preserves(j, cte_scope=local)
            combined = combined if preserved else frozenset[_QKey]()
            joins_preserve = joins_preserve and preserved

        group = sg.group_of(sel)
        grouped = group is not None and bool(group.expressions)
        if group is not None and grouped:
            gk = _group_key(group, from_alias=from_alias)
            combined = gk if gk is not None else frozenset[_QKey]()

        projection = _Projection.build(sel, from_alias=from_alias)
        keys = _project(sel, combined, projection)
        conditional: frozenset[ConditionalKey] = frozenset()
        # Conditional keys ride through only where the row set is not multiplied and
        # their columns stay disambiguated: a single source, or a non-multiplying join
        # under a star-free projection (every output column then resolves to one
        # source by alias). A star over a join could blur a from-side column with the
        # joined-in side, so it drops, matching the predicate flow's own caution.
        star_free = not (projection.unrestricted or projection.star_aliases)
        if not grouped and (not joins or (joins_preserve and star_free)):
            conditional = _carry_conditional(from_carried.conditional, projection, from_alias)
        return _Carried(keys, conditional)

    def _union(self, u: exp.Union, *, cte_scope: Mapping[str, _Carried]) -> _Carried:
        left = u.this
        right = u.args.get("expression")
        if isinstance(left, Expr):
            self.scope_keys(left, cte_scope=cte_scope)
        if isinstance(right, Expr):
            self.scope_keys(right, cte_scope=cte_scope)
        # UNION ALL concatenates, so a key on both arms still need not hold on the
        # result (the same value can appear in both). UNION (distinct) dedupes the
        # full projected tuple, which is therefore a key. Conditional keys drop: the
        # arms may carry different predicates.
        if not bool(u.args.get("distinct")) or not isinstance(left, exp.Select):
            return _EMPTY
        names = _output_names(left)
        return _Carried(frozenset({frozenset(names)}) if names else frozenset())

    def _resolve_source(
        self, node: Expr, *, cte_scope: Mapping[str, _Carried]
    ) -> tuple[str, _Carried] | None:
        if isinstance(node, exp.Table):
            alias = node.alias_or_name
            name = node.name
            if name in cte_scope:
                return alias, cte_scope[name]
            return alias, self._base_resolve(node)
        if isinstance(node, exp.Subquery):
            inner = node.this
            alias = node.alias_or_name
            if not isinstance(inner, Expr) or not alias:
                return None
            return alias, self.scope_keys(inner, cte_scope=cte_scope)
        return None

    def _join_preserves(self, j: exp.Join, *, cte_scope: Mapping[str, _Carried]) -> bool:
        """Whether ``j`` cannot multiply the probe side's rows, so the probe side's
        keys (and any conditional keys riding with them) carry through unchanged.

        The joined-in side cannot multiply probe rows exactly when its join columns
        cover one of its keys. A CROSS join, an unresolved target, a keyless joined-in
        side, or a missing / non-covering ON all leave fanout possible, so none of the
        probe side's keys can be trusted to survive.
        """
        if sg.join_side_of(j) is JoinSide.CROSS:
            return False  # explicit cartesian product: no key survives
        target = j.this
        if not isinstance(target, Expr):
            return False
        resolved = self._resolve_source(target, cte_scope=cte_scope)
        if resolved is None:
            return False
        r_alias, r_carried = resolved
        if not r_carried.keys:
            return False  # joined-in side has no known key: can't rule out fanout
        on = sg.on_of(j)
        if on is None:
            return False
        right_join_cols = sg.equality_cols_on_alias(on, r_alias)
        if right_join_cols is None:
            return False
        return any(k <= right_join_cols for k in r_carried.keys)


def _qualify(alias: str, keys: frozenset[Key]) -> frozenset[_QKey]:
    """Lift a source's bare keys into alias-qualified keys for the working scope."""
    return frozenset(frozenset((alias, col) for col in key) for key in keys)


def _group_key(group: exp.Group, *, from_alias: str) -> frozenset[_QKey] | None:
    """The key a GROUP BY introduces, or ``None`` for a shape we cannot size
    (positional or expression group keys), which drops tracked keys."""
    cols: list[_QCol] = []
    for g in group.expressions:
        if not isinstance(g, exp.Column):
            return None
        cols.append((sg.column_table(g) or from_alias, sg.column_name(g)))
    return frozenset({frozenset(cols)})


def _output_names(sel: exp.Select) -> list[str]:
    """Output column names from a projection list, for sizing DISTINCT / UNION keys.

    Counts only projections resolving to a named output column: bare columns and
    aliases. A computed projection without a name is skipped.
    """
    names: list[str] = []
    for proj in sel.expressions:
        if isinstance(proj, exp.Alias):
            names.append(proj.alias_or_name.lower())
        elif isinstance(proj, exp.Column) and not isinstance(proj.this, exp.Star):
            names.append(sg.column_name(proj).lower())
    return names


def _project(
    sel: exp.Select, combined: frozenset[_QKey], projection: _Projection
) -> frozenset[Key]:
    """Map the scope's qualified keys onto bare output-column names, then add the
    DISTINCT full-tuple key when present."""
    out: set[Key] = set()
    for qkey in combined:
        mapped = projection.map_key(qkey)
        if mapped is not None:
            out.add(mapped)
    if sel.args.get("distinct") is not None:
        names = _output_names(sel)
        if names:
            out.add(frozenset(names))
    return frozenset(out)


def _qualify_key(alias: str, key: Key) -> _QKey:
    """Lift one bare key into an alias-qualified key for the working scope."""
    return frozenset((alias, col) for col in key)


def _carry_conditional(
    conditional: frozenset[ConditionalKey], projection: _Projection, from_alias: str
) -> frozenset[ConditionalKey]:
    """Carry the source's conditional keys onto this scope's output columns.

    Both the key columns and the predicate columns rename through the same
    projection, so the carried predicate stays in the same column space the predicate
    flow uses; activation can then match them. A conditional key whose key column or
    any predicate column does not survive the projection is dropped.
    """
    out: set[ConditionalKey] = set()
    for ck in conditional:
        mapped_key = projection.map_key(_qualify_key(from_alias, ck.key))
        if mapped_key is None:
            continue
        mapped_predicate = _carry_predicate(ck.predicate, projection, from_alias)
        if mapped_predicate is None:
            continue
        out.add(ConditionalKey(mapped_key, mapped_predicate))
    return frozenset(out)


def _carry_predicate(
    predicate: frozenset[Canon], projection: _Projection, from_alias: str
) -> frozenset[Canon] | None:
    """Rename a conditional key's predicate through the projection, or ``None`` if any
    atom cannot be carried (its column is dropped, or it is opaque under an explicit
    projection). All-or-nothing: dropping an atom would weaken the predicate and let
    the key activate too readily, so the whole conditional key drops instead."""
    if projection.unrestricted or from_alias in projection.star_aliases:
        return predicate  # full passthrough: every column survives under its own name
    out: set[Canon] = set()
    for atom in predicate:
        if not isinstance(atom, CmpAtom | InAtom):
            return None  # opaque: its column is unknown, so it cannot be tracked
        col = atom_column(atom)
        if col is None:
            return None
        names = projection.names_for((from_alias, col))
        if not names:
            return None
        # A column may project to several output names. The predicate flow emits the
        # renamed atom under *every* one of them, so a single representative here is
        # always a member of the flow's atom set and activation's entailment matches
        # it regardless of which we pick; ``min`` just keeps the choice deterministic.
        out.add(rename_atom(atom, min(names)))
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class _Projection:
    """The output-name mapping a SELECT projection induces.

    ``aliased`` maps each input qualified column to the output names it appears
    under. ``star_aliases`` are aliases whose ``alias.*`` appears; ``unrestricted``
    is set when a bare ``*`` appears; both let columns pass through under their
    base name. ``ambiguous`` are output names that resolve to more than one input
    column, so a key using them cannot be projected safely.
    """

    aliased: Mapping[_QCol, tuple[str, ...]]
    star_aliases: frozenset[str]
    unrestricted: bool
    ambiguous: frozenset[str]

    @staticmethod
    def build(sel: exp.Select, *, from_alias: str) -> _Projection:
        aliased: dict[_QCol, list[str]] = {}
        star_aliases: set[str] = set()
        unrestricted = False
        seen: dict[str, _QCol] = {}
        ambiguous: set[str] = set()

        def note(name: str, qc: _QCol) -> None:
            prior = seen.get(name)
            if prior is None:
                seen[name] = qc
            elif prior != qc:
                ambiguous.add(name)

        for proj in sel.expressions:
            if isinstance(proj, exp.Star):
                unrestricted = True
            elif isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                star_aliases.add(proj.table or from_alias)
            elif isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Column):
                qc: _QCol = (sg.column_table(proj.this) or from_alias, sg.column_name(proj.this))
                name = proj.alias_or_name.lower()
                aliased.setdefault(qc, []).append(name)
                note(name, qc)
            elif isinstance(proj, exp.Column) and not isinstance(proj.this, exp.Star):
                qc = (sg.column_table(proj) or from_alias, sg.column_name(proj))
                name = sg.column_name(proj).lower()
                aliased.setdefault(qc, []).append(name)
                note(name, qc)
            # Other shapes (unaliased computed expressions, windows) produce no
            # tractable output name; a key resting on them simply will not map.

        return _Projection(
            aliased={qc: tuple(names) for qc, names in aliased.items()},
            star_aliases=frozenset(star_aliases),
            unrestricted=unrestricted,
            ambiguous=frozenset(ambiguous),
        )

    def map_key(self, key: _QKey) -> Key | None:
        """The output key a qualified key projects to, or ``None`` if any of its
        columns does not survive the projection."""
        out: set[str] = set()
        for qc in key:
            names = self.names_for(qc)
            if not names:
                return None
            out.add(min(names))  # one occurrence suffices; pick a stable representative
        return frozenset(out)

    def names_for(self, qc: _QCol) -> list[str]:
        out: list[str] = []
        if qc in self.aliased:
            out.extend(n for n in self.aliased[qc] if n not in self.ambiguous)
        if (self.unrestricted or qc[0] in self.star_aliases) and qc[1] not in self.ambiguous:
            out.append(qc[1])
        return out


# --- the property ------------------------------------------------------------


def uniqueness_property(
    manifest: Manifest,
    *,
    extra: tuple[FactDiscoverer[CandidateKeySet, SourceRef], ...] = (),
) -> Property[CandidateKeySet, SourceRef]:
    """The manifest-backed uniqueness property: declared keys (unique tests,
    ``unique_combination_of_columns``, native PRIMARY KEY / UNIQUE, plus any
    ``extra``) ground each relation, and the relation reducer infers more from the
    SQL. Declared and inferred keys both hold, so they compose by meet
    (``reconcile_by_meet``); no opaque opt-out reader is wired yet, so the opaque
    set is empty. The property carries its relation-algebra walk as ``reducer`` so
    the propagator dispatches it without a global registry."""
    discoverers = (
        unique_test_discoverer(),
        unique_combination_discoverer(),
        native_key_discoverer(manifest.adapter_type),
        *extra,
    )
    # The uniqueness discoverers ground against the manifest directly, so they
    # need no name-to-source map; pass an empty one to the shared collector.
    facts = collect(manifest, discoverers, name_to_source={})
    return relation_property(
        name="uniqueness",
        lattice=UNIQUENESS_LATTICE,
        operators={},
        aggregates={},
        ground=_grounding_with_conditional(facts),
        reconcile_by_meet=True,
        reducer=relation_reduce,
    )


def _grounding_with_conditional(
    facts: Mapping[SourceRef, tuple[Fact[CandidateKeySet, SourceRef], ...]],
) -> Callable[[SourceRef], Annotation[CandidateKeySet]]:
    """The shared grounding, extended to carry each scope's conditional keys.

    The shared ``grounding`` folds only unconditional facts, so a ``where``-filtered
    ``unique`` would ground nothing. Here those conditional facts become the value's
    ``conditional`` payload, marked CONCRETE so reconcile keeps it (an IMPLICIT
    grounded value is discarded in favour of the inferred one). The payload rides
    along until activation promotes it; it never counts as an unconditional key.
    """
    base = grounding(facts, opaque=set(), lat=UNIQUENESS_LATTICE)
    conditional = _conditional_by_scope(facts)

    def ground(scope: SourceRef) -> Annotation[CandidateKeySet]:
        ann = base(scope)
        cond = conditional.get(scope)
        if cond is None or ann.opacity is Opacity.EXPLICIT:
            return ann
        return Annotation(CandidateKeySet(ann.value.keys, cond), Opacity.CONCRETE, ann.provisional)

    return ground


def _conditional_by_scope(
    facts: Mapping[SourceRef, tuple[Fact[CandidateKeySet, SourceRef], ...]],
) -> dict[SourceRef, frozenset[ConditionalKey]]:
    """The conditional candidate keys captured per scope, with each test's ``where``
    parsed to atoms. A predicate that does not parse carries no information, so its
    key is dropped rather than activated on a guess."""
    out: dict[SourceRef, frozenset[ConditionalKey]] = {}
    for scope, bucket in facts.items():
        cks: set[ConditionalKey] = set()
        for fact in bucket:
            if fact.condition is None:
                continue
            parsed = parse_predicate(fact.condition.sql)
            if parsed is None:
                continue
            predicate = atoms_of(parsed)
            cks.update(ConditionalKey(key, predicate) for key in fact.value.keys)
        if cks:
            out[scope] = frozenset(cks)
    return out


def activate_conditional(
    keys: Mapping[SourceRef, Annotation[CandidateKeySet]],
    flow: Mapping[SourceRef, Annotation[RowFilter]],
) -> dict[SourceRef, CandidateKeySet]:
    """Promote each relation's conditional keys whose predicate its flowed filter
    implies, leaving the carried conditional payload in place for scopes downstream.

    The promoted key folds in by the uniqueness ``meet`` (a union), exactly as a
    declared key would, so a relation that defines its own filter and carries a
    matching ``where``-filtered ``unique`` gains that key unconditionally.
    """
    out: dict[SourceRef, CandidateKeySet] = {}
    for ref, ann in keys.items():
        value = ann.value
        flow_ann = flow.get(ref)
        flow_atoms = flow_ann.value.atoms if flow_ann is not None else frozenset[Canon]()
        promoted = activate(
            value,
            ((CandidateKeySet.of(ck.key), ck.predicate) for ck in value.conditional),
            flow_atoms,
            _meet,
        )
        out[ref] = promoted
    return out
