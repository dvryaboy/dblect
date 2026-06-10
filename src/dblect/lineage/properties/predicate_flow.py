"""Predicate-flow property: the row filter every row of a relation satisfies.

A relation's value is the conjunction of atoms (in the relation's *output* column
names) that every one of its rows is known to satisfy. A ``WHERE`` conjoins its
atoms, a passthrough carries the upstream filter, a consumer's own ``WHERE`` adds
to it, and a projection renames the filter's columns (``country = 'US'`` becomes
``region = 'US'`` after ``country AS region``). CTEs and inline subqueries
accumulate for free, since the relation walk recurses through them.

The property is the shared substrate a later activation step reads to decide when a
captured conditional fact applies: a conditional ``unique`` / ``not_null`` holds at
any scope whose accumulated filter *implies* the test's predicate. Type refinement
and contract validation are further consumers of the same flow.

The value reuses the predicate engine's typed atoms (:data:`Canon`), so the filter
is rigorously shaped and feeds the engine directly. Posture is silent-when-unproven:
a shape the walk cannot carry soundly (a ``JOIN`` whose columns could blur across
sources, a ``UNION`` whose arms differ, a ``GROUP BY`` that changes row identity, a
filtered column the projection drops) yields "no filter known" rather than a guess.
The lattice orders by precision, where knowing *more* atoms is more precise: ``meet``
unions the atom sets, ``join`` (confluence) keeps the atoms both branches carry, and
``top`` is the empty set. Filters are never declared, only derived, so ``ground``
returns ``top`` everywhere and the reducer does all the work.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Opacity
from dblect.lineage.facts.property import DepContext, Property, relation_property
from dblect.lineage.graph import SourceRef, source_ref_meta
from dblect.lineage.predicate import Canon, CmpAtom, InAtom, atom_column, atoms_of, rename_atom
from dblect.sql import _sqlglot as sg


@dataclass(frozen=True, slots=True)
class RowFilter:
    """The conjunction of atoms every row of a relation is known to satisfy.

    ``atoms`` holds the known conjuncts in the relation's output column names; the
    empty set is the lattice ``top`` ("no filter known"). ``is_bottom`` marks the
    formal universal element (the lattice ``bottom``): it absorbs under ``meet`` and
    is the identity under ``join``. Filters only ever accumulate, so no resolution of
    real derivations reaches bottom; it exists only so the lattice is bounded.
    """

    atoms: frozenset[Canon]
    is_bottom: bool = False

    @staticmethod
    def of(*atoms: Canon) -> RowFilter:
        return RowFilter(frozenset(atoms))


# The value every relation grounds to ("no filter known"), and the meet identity.
NO_FILTER: RowFilter = RowFilter(frozenset())

# The formal universal element, present so the lattice is bounded; unreachable when
# reducing real derivations, which only ever union atoms.
ALL_FILTERS: RowFilter = RowFilter(frozenset(), is_bottom=True)


def _meet(a: RowFilter, b: RowFilter) -> RowFilter:
    """Most precise filter consistent with both: the union of their atoms."""
    if a.is_bottom or b.is_bottom:
        return ALL_FILTERS
    return RowFilter(a.atoms | b.atoms)


def _join(a: RowFilter, b: RowFilter) -> RowFilter:
    """Least precise filter both refine: the atoms both sides carry (intersection)."""
    if a.is_bottom:
        return b
    if b.is_bottom:
        return a
    return RowFilter(a.atoms & b.atoms)


PREDICATE_FLOW_LATTICE: Lattice[RowFilter] = Lattice(
    meet=_meet,
    join=_join,
    top=NO_FILTER,
    bottom=ALL_FILTERS,
)


# --- the relation reducer ----------------------------------------------------
#
# The relation-algebra walk for row filters: the same shape as the uniqueness walk
# (reduce a derivation to an inferred annotation, recursing into referenced nodes),
# accumulating a filter rather than keys. The per-case rules are in the module
# docstring; each case below carries the reason it carries or drops.

# Resolves a base (non-CTE) table's accumulated filter via its stamped SourceRef.
_BaseFilter = Callable[["exp.Table"], frozenset[Canon]]


def _flow_reduce(
    deriv: Expr,
    _prop: Property[RowFilter, SourceRef],
    recurse: Callable[[SourceRef], Annotation[RowFilter]],
    _ctx: DepContext,
    _default: Annotation[RowFilter],
) -> Annotation[RowFilter]:
    """Reduce a model's relational tree to its inferred row filter.

    A base table resolves through ``recurse`` on its stamped ``SourceRef``, so the
    upstream filter and its provisional taint flow in. CTEs and inline subqueries are
    resolved structurally within the walk.
    """
    provisional = False

    def base_filter(table: exp.Table) -> frozenset[Canon]:
        nonlocal provisional
        ref = source_ref_meta(table)
        if ref is None:
            return frozenset()
        ann = recurse(ref)
        provisional = provisional or ann.provisional
        return ann.value.atoms

    atoms = _FlowWalk(base_filter).scope_filter(deriv, cte_scope={})
    opacity = Opacity.CONCRETE if atoms else Opacity.IMPLICIT
    return Annotation(RowFilter(atoms), opacity, provisional=provisional)


class _FlowWalk:
    """Bottom-up row-filter inference over one relational tree.

    ``base_filter`` resolves a base table's filter; CTEs and inline subqueries are
    resolved structurally within the walk. A scope is carried only when it is a
    single source with no join; everything else drops to the empty filter. With
    ``record`` set, every scope's filter is kept in ``scopes`` keyed by ``id(node)``,
    so a detector can read the filter in force at an intermediate CTE / subquery.
    """

    def __init__(self, base_filter: _BaseFilter, *, record: bool = False) -> None:
        self._base_filter = base_filter
        self._record = record
        self.scopes: dict[int, frozenset[Canon]] = {}

    def scope_filter(
        self, node: Expr, *, cte_scope: Mapping[str, frozenset[Canon]]
    ) -> frozenset[Canon]:
        if isinstance(node, exp.Select):
            atoms = self._select(node, cte_scope=cte_scope)
        else:
            atoms = frozenset[Canon]()  # UNION (arms may differ) and non-SELECTs carry nothing
        if self._record:
            self.scopes[id(node)] = atoms
        return atoms

    def _select(
        self, sel: exp.Select, *, cte_scope: Mapping[str, frozenset[Canon]]
    ) -> frozenset[Canon]:
        local = dict(cte_scope)
        with_ = sel.args.get("with_")
        if isinstance(with_, exp.With):
            for cte in with_.expressions:
                if isinstance(cte, exp.CTE) and isinstance(cte.this, Expr):
                    local[cte.alias_or_name] = self.scope_filter(cte.this, cte_scope=local)

        from_ = sg.from_of(sel)
        if from_ is None or not isinstance(from_.this, Expr):
            return frozenset()
        if sg.joins_of(sel):
            return frozenset()  # multi-source: a bare-name atom could blur across sources
        source = self._resolve_source(from_.this, cte_scope=local)
        if source is None:
            return frozenset()

        group = sg.group_of(sel)
        if group is not None and group.expressions:
            return frozenset()  # GROUP BY changes row identity; the input filter no longer applies

        where = sg.where_of(sel)
        where_atoms: frozenset[Canon] = (
            atoms_of(where.this)
            if where is not None and isinstance(where.this, Expr)
            else frozenset()
        )
        return _project_filter(sel, source | where_atoms)

    def _resolve_source(
        self, node: Expr, *, cte_scope: Mapping[str, frozenset[Canon]]
    ) -> frozenset[Canon] | None:
        if isinstance(node, exp.Table):
            if node.name in cte_scope:
                return cte_scope[node.name]
            return self._base_filter(node)
        if isinstance(node, exp.Subquery):
            inner = node.this
            if not isinstance(inner, Expr):
                return None
            return self.scope_filter(inner, cte_scope=cte_scope)
        return None


def _project_filter(sel: exp.Select, atoms: frozenset[Canon]) -> frozenset[Canon]:
    """Map the scope's input-column atoms onto its output-column names.

    Under a ``*`` every column passes through unchanged, so every atom carries
    verbatim (a bare-boolean opaque atom included). Under an explicit projection a
    comparison/``IN`` atom renames to the output name(s) its column appears under and
    drops if its column has no image; an opaque atom drops, since its column is
    unknown and cannot be tracked through the rename.
    """
    if has_star(sel):
        return atoms
    rename = explicit_rename(sel)
    out: set[Canon] = set()
    for atom in atoms:
        if not isinstance(atom, CmpAtom | InAtom):
            continue
        col = atom_column(atom)
        if col is None:
            continue
        for name in rename.get(col, ()):
            out.add(rename_atom(atom, name))
    return frozenset(out)


def has_star(sel: exp.Select) -> bool:
    for proj in sel.expressions:
        if isinstance(proj, exp.Star):
            return True
        if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
            return True
    return False


def explicit_rename(sel: exp.Select) -> Mapping[str, tuple[str, ...]]:
    """Each input column's output name(s) under an explicit (star-free) projection.

    An output name that more than one input column resolves to is ambiguous and is
    dropped, since an atom renamed onto it could no longer be traced to one column.
    """
    raw: dict[str, list[str]] = {}
    out_to_in: dict[str, set[str]] = {}
    for proj in sel.expressions:
        pair = _projection_pair(proj)
        if pair is None:
            continue
        input_col, output_name = pair
        raw.setdefault(input_col, []).append(output_name)
        out_to_in.setdefault(output_name, set()).add(input_col)
    ambiguous = {name for name, inputs in out_to_in.items() if len(inputs) > 1}
    return {col: tuple(n for n in names if n not in ambiguous) for col, names in raw.items()}


def _projection_pair(proj: Expr) -> tuple[str, str] | None:
    """The ``(input column, output name)`` a projection induces, or ``None`` for a
    shape with no single tractable input column (a computed expression, a star)."""
    if isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Column):
        inner = proj.this
        if isinstance(inner.this, exp.Star):
            return None
        return (sg.column_name(inner).lower(), proj.alias_or_name.lower())
    if isinstance(proj, exp.Column) and not isinstance(proj.this, exp.Star):
        name = sg.column_name(proj).lower()
        return (name, name)
    return None


def relation_scope_filters(
    tree: Expr, model_flow: Mapping[str, RowFilter]
) -> Mapping[int, frozenset[Canon]]:
    """Per-scope row filter for every SELECT/UNION node in ``tree``, keyed by
    ``id(node)``.

    The same flow algebra the reducer runs, but for one already-parsed tree with base
    tables resolved by name against ``model_flow`` (the per-model filters propagation
    produced). This is what a detector consults to learn the filter in force at an
    intermediate CTE or inline subquery, so it can activate a conditional key there.
    The returned map is valid only for the lifetime of ``tree``.
    """

    def base_filter(table: exp.Table) -> frozenset[Canon]:
        return model_flow.get(table.name, NO_FILTER).atoms

    walk = _FlowWalk(base_filter, record=True)
    walk.scope_filter(tree, cte_scope={})
    return walk.scopes


# --- the property ------------------------------------------------------------


def _ground(_scope: SourceRef) -> Annotation[RowFilter]:
    """A relation's filter is never declared, only derived, so every scope grounds to
    the empty filter and the reducer supplies all information."""
    return Annotation(NO_FILTER, Opacity.IMPLICIT)


def predicate_flow_property() -> Property[RowFilter, SourceRef]:
    """The predicate-flow property: each relation's accumulated row filter, inferred
    by the relation reducer. Nothing grounds it (filters are derived), and atoms only
    accumulate, so declared and inferred compose by meet (``reconcile_by_meet``). The
    property carries its relation-algebra walk as ``reducer`` so the propagator
    dispatches it without a global registry."""
    return relation_property(
        name="predicate_flow",
        lattice=PREDICATE_FLOW_LATTICE,
        operators={},
        aggregates={},
        ground=_ground,
        reconcile_by_meet=True,
        reducer=_flow_reduce,
    )
