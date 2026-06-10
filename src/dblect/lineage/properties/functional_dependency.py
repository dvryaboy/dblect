"""Functional-dependency property: the dependencies a relation's rows satisfy.

A relation's value is a set of functional dependencies over its output column
names, each one ``X -> y``: rows equal on ``X`` are equal on ``y``. This is the
discharge substrate the aggregate coherence guard reads. ``sum(amount) group by
country`` over a per-row currency is well typed exactly when the group key holds
the currency constant per group, and a ``country -> currency`` dependency is the
summarizability argument for that (Lenz & Shoshani, SSDBM 1997; Hurtado &
Mendelzon, ICDT 2001). Entailment (:func:`determines`) is attribute closure under
Armstrong's axioms.

The lattice orders by precision exactly as uniqueness orders keys: knowing more
dependencies is more precise, so ``meet`` (resolution of declarations) unions the
sets, ``join`` (confluence) intersects them, ``top`` is the empty set, and
``bottom`` is a formal universal element no real resolution reaches.

Dependencies come from four places. A declaration grounds one directly (synthetic
facts until the authoring bridge lands; the ``determines(...)`` contract is its
eventual source). An equality filter pins a column constant, the empty-determinant
dependency. A GROUP BY makes its group key determine every output (the key of the
grouped result). And a candidate key read from the uniqueness property determines
every column selected alongside it, since a relation unique on ``K`` admits one
row per ``K`` value. Posture elsewhere is silent-when-unproven: a join can pair
determinant values across sources and two UNION arms can each satisfy a dependency
their union violates, so both prove nothing (the join transfer is a later build,
alongside the rest of the join concerns).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.grounding import grounding
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Fact, Opacity
from dblect.lineage.facts.property import DepContext, Property, PropertyRef, relation_property
from dblect.lineage.graph import SourceRef, source_ref_meta
from dblect.lineage.properties.predicate_flow import explicit_rename, has_star
from dblect.lineage.properties.uniqueness import CandidateKeySet, Key
from dblect.sql import _sqlglot as sg

# --- the value type ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FD:
    """One dependency over a relation's output column names, in canonical
    single-dependent form (``X -> yz`` splits into ``X -> y`` and ``X -> z``).
    Names are case-folded to match the graph. An empty determinant says the
    dependent is constant over the whole relation, the strongest claim."""

    determinant: frozenset[str]
    dependent: str


@dataclass(frozen=True, slots=True)
class FDSet:
    """The dependencies a relation is known to satisfy.

    ``fds`` holds the known dependencies; the empty set is the lattice ``top``
    ("no dependency known"). ``is_bottom`` marks the formal universal element (the
    lattice ``bottom``): it absorbs under ``meet`` and is the identity under
    ``join``, and no resolution of real declarations reaches it, since dependency
    claims only ever union. Equality is structural, so ``FDSet(frozenset())``
    (top) and the bottom sentinel are distinct values.
    """

    fds: frozenset[FD]
    is_bottom: bool = False

    @staticmethod
    def of(*fds: FD) -> FDSet:
        return FDSet(frozenset(fds))


# The empty dependency set: "we know of no dependency", the value every
# undeclared relation grounds to and the meet identity.
NO_FDS: FDSet = FDSet(frozenset())

# The formal universal element. Unreachable when resolving real declarations
# (they only union), present so the lattice is bounded.
ALL_FDS: FDSet = FDSet(frozenset(), is_bottom=True)


def _meet(a: FDSet, b: FDSet) -> FDSet:
    """Most precise value consistent with both: the union of the dependencies."""
    if a.is_bottom or b.is_bottom:
        return ALL_FDS
    return FDSet(a.fds | b.fds)


def _join(a: FDSet, b: FDSet) -> FDSet:
    """Least precise value both refine: the dependencies both sides carry."""
    if a.is_bottom:
        return b
    if b.is_bottom:
        return a
    return FDSet(a.fds & b.fds)


FUNCTIONAL_DEPENDENCY_LATTICE: Lattice[FDSet] = Lattice(
    meet=_meet,
    join=_join,
    top=NO_FDS,
    bottom=ALL_FDS,
)


def determines(value: FDSet, given: frozenset[str], target: str) -> bool:
    """Whether ``value`` entails ``given -> target``: attribute closure under
    Armstrong's axioms (sound and complete for FD entailment). The bottom sentinel
    entails everything, and a target inside ``given`` holds by reflexivity."""
    if target in given:
        return True
    if value.is_bottom:
        return True
    closure = set(given)
    changed = True
    while changed:
        changed = False
        for fd in value.fds:
            if fd.dependent not in closure and fd.determinant <= closure:
                closure.add(fd.dependent)
                changed = True
    return target in closure


# --- grounding -----------------------------------------------------------------


def functional_dependency_grounding(
    facts: Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]],
    *,
    opaque: Collection[SourceRef] = (),
) -> Callable[[SourceRef], Annotation[FDSet]]:
    """Fold the per-relation dependency facts into grounded annotations. The same
    fold every property uses: an opt-out grounds EXPLICIT top, a resolved bucket
    grounds its value CONCRETE, everything else the IMPLICIT-top default."""
    return grounding(facts, opaque, FUNCTIONAL_DEPENDENCY_LATTICE)


# --- the relation reducer --------------------------------------------------------
#
# The relation-algebra walk for dependencies, the same shape as the predicate-flow
# walk: single-source scopes carry, rename through the projection, and every shape
# outside the modelled fragment drops to the empty set rather than over-claiming.


@dataclass(frozen=True, slots=True)
class _Base:
    """What a resolved FROM source contributes: its dependencies (in its output
    column names) and, for a base table with the uniqueness edge live, its
    candidate keys (each of which determines the columns read alongside it)."""

    fds: frozenset[FD]
    keys: frozenset[Key] = frozenset()


_NOTHING: _Base = _Base(frozenset())

# Resolves a base (non-CTE) table reference to what it contributes. The reducer's
# implementation recurses through the shared propagator via the table's stamped
# SourceRef, so declarations and the provisional taint flow across models.
_BaseResolve = Callable[["exp.Table"], _Base]


class _FdWalk:
    """Bottom-up dependency inference over one relational tree.

    ``base_resolve`` resolves a base table; CTEs and inline subqueries are
    resolved structurally within the walk. Only single-source scopes carry:
    a join or a UNION proves nothing, and an unmodellable group shape (positional
    or computed group keys) drops the scope's dependencies entirely.
    """

    def __init__(self, base_resolve: _BaseResolve) -> None:
        self._base_resolve = base_resolve

    def scope_fds(self, node: Expr, *, cte_scope: Mapping[str, frozenset[FD]]) -> frozenset[FD]:
        if isinstance(node, exp.Select):
            return self._select(node, cte_scope=cte_scope)
        return frozenset()

    def _select(self, sel: exp.Select, *, cte_scope: Mapping[str, frozenset[FD]]) -> frozenset[FD]:
        local = dict(cte_scope)
        with_ = sel.args.get("with_")
        if isinstance(with_, exp.With):
            for cte in with_.expressions:
                if isinstance(cte, exp.CTE) and isinstance(cte.this, Expr):
                    local[cte.alias_or_name] = self.scope_fds(cte.this, cte_scope=local)

        from_ = sg.from_of(sel)
        if from_ is None or not isinstance(from_.this, Expr):
            return frozenset()
        if sg.joins_of(sel):
            return frozenset()  # a join can pair determinant values across sources
        base = self._resolve_source(from_.this, cte_scope=local)
        if base is None:
            return frozenset()
        carried = base.fds

        # A dependency is universally quantified over row pairs, and a WHERE only
        # removes pairs, so everything carries; an equality filter additionally pins
        # its column constant, the empty-determinant dependency.
        where = sg.where_of(sel)
        if where is not None and isinstance(where.this, Expr):
            carried = carried | {
                FD(frozenset(), sg.column_name(col).lower())
                for col in sg.equality_literal_columns(where.this)
            }

        star = has_star(sel)
        rename = explicit_rename(sel)

        # A relation unique on K admits one row per K value, so K determines every
        # column this scope reads from it. Minted only over named projections: under
        # a bare star the column universe is unknown.
        for key in base.keys:
            carried = carried | {FD(key, dep) for dep in rename if dep not in key}

        group = sg.group_of(sel)
        group_names: frozenset[str] | None = None
        if group is not None and group.expressions:
            group_names = _group_columns(group)
            if group_names is None:
                return frozenset()  # unmodellable group shape: prove nothing
            # Grouping aggregates everything outside the group key away, so only a
            # dependency lying entirely within it still describes the output rows.
            carried = frozenset(
                fd for fd in carried if fd.determinant | {fd.dependent} <= group_names
            )

        out = carried if star else _remap(carried, rename)
        if group_names is not None:
            group_out = group_names if star else _remap_columns(group_names, rename)
            if group_out is not None:
                # The group key is a key of the grouped result (one row per group),
                # so it determines every named output.
                out = out | {
                    FD(group_out, name) for name in _named_outputs(sel) if name not in group_out
                }
        return out

    def _resolve_source(
        self, node: Expr, *, cte_scope: Mapping[str, frozenset[FD]]
    ) -> _Base | None:
        if isinstance(node, exp.Table):
            if node.name in cte_scope:
                return _Base(cte_scope[node.name])
            return self._base_resolve(node)
        if isinstance(node, exp.Subquery):
            inner = node.this
            if not isinstance(inner, Expr):
                return None
            return _Base(self.scope_fds(inner, cte_scope=cte_scope))
        return None


def _group_columns(group: exp.Group) -> frozenset[str] | None:
    """The group key as case-folded input column names, or ``None`` for a shape we
    cannot name (positional or expression group keys)."""
    out: set[str] = set()
    for g in group.expressions:
        if not isinstance(g, exp.Column) or isinstance(g.this, exp.Star):
            return None
        out.add(sg.column_name(g).lower())
    return frozenset(out)


def _remap(fds: frozenset[FD], rename: Mapping[str, tuple[str, ...]]) -> frozenset[FD]:
    """Rename each dependency onto the projection's output names, dropping any whose
    columns do not all survive. One output name per input column suffices (copies of
    a column carry equal values), picked stably."""
    out: set[FD] = set()
    for fd in fds:
        determinant = _remap_columns(fd.determinant, rename)
        dependent = rename.get(fd.dependent)
        if determinant is None or not dependent:
            continue
        out.add(FD(determinant, min(dependent)))
    return frozenset(out)


def _remap_columns(
    columns: frozenset[str], rename: Mapping[str, tuple[str, ...]]
) -> frozenset[str] | None:
    out: set[str] = set()
    for col in columns:
        names = rename.get(col)
        if not names:
            return None
        out.add(min(names))
    return frozenset(out)


def _named_outputs(sel: exp.Select) -> frozenset[str]:
    """Every output column the projection names: bare columns and aliases, computed
    projections included (an aggregate's alias is exactly what a group key
    determines). Unnamed shapes contribute nothing."""
    names: set[str] = set()
    for proj in sel.expressions:
        if isinstance(proj, exp.Alias):
            names.add(proj.alias_or_name.lower())
        elif isinstance(proj, exp.Column) and not isinstance(proj.this, exp.Star):
            names.add(sg.column_name(proj).lower())
    return frozenset(names)


# --- the property ------------------------------------------------------------


def functional_dependency_property(
    ground: Callable[[SourceRef], Annotation[FDSet]],
    *,
    uniqueness: PropertyRef[CandidateKeySet, SourceRef] | None = None,
) -> Property[FDSet, SourceRef]:
    """The relation-scoped functional-dependency property over a caller-supplied
    grounding (synthetic facts in tests; the contract bridge is the eventual
    source). Declared and inferred dependencies both hold, so they compose by meet
    (``reconcile_by_meet``), exactly as uniqueness composes keys. Passing the
    uniqueness property's ref switches on the key-derived source and declares the
    dependency edge the registry orders by."""

    def reduce_(
        deriv: Expr,
        _prop: Property[FDSet, SourceRef],
        recurse: Callable[[SourceRef], Annotation[FDSet]],
        ctx: DepContext,
        _default: Annotation[FDSet],
    ) -> Annotation[FDSet]:
        provisional = False

        def base_resolve(table: exp.Table) -> _Base:
            nonlocal provisional
            ref = source_ref_meta(table)
            if ref is None:
                return _NOTHING
            ann = recurse(ref)
            provisional = provisional or ann.provisional
            keys: frozenset[Key] = frozenset()
            if uniqueness is not None:
                key_ann = ctx.annotation(uniqueness, ref)
                if key_ann is not None:
                    keys = key_ann.value.keys
            return _Base(ann.value.fds, keys)

        fds = _FdWalk(base_resolve).scope_fds(deriv, cte_scope={})
        opacity = Opacity.CONCRETE if fds else Opacity.IMPLICIT
        return Annotation(FDSet(fds), opacity, provisional=provisional)

    return relation_property(
        name="functional_dependency",
        lattice=FUNCTIONAL_DEPENDENCY_LATTICE,
        operators={},
        aggregates={},
        ground=ground,
        depends_on=(uniqueness,) if uniqueness is not None else (),
        reconcile_by_meet=True,
        reducer=reduce_,
    )
