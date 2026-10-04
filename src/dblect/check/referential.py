"""Referential orphan drop: a declared foreign key turns an ordinary join into
something checkable.

A join that discards rows with no match on the other side is ordinary SQL,
correct almost everywhere, so flagging it undeclared is noise. Declare a column a
foreign key, though, and an inner join across it stops being ambiguous: every
child row is expected to match a parent, so a silent drop is now worth
reporting.

This is a direct structural read over the resolved, ``ColumnRef``-stamped tree,
gated on a declaration: there is no derived value to compare against a
declaration here, only a join site and a declared edge. :func:`orphan_drop_sites`
is the pure signal; ``check/run.py`` turns its output into findings.

Left undetected, by design, rather than half-handled: a ``LEFT JOIN ...
WHERE parent.col = x`` that re-filters the padded side to a literal (turning the
LEFT into an effective INNER); a later join whose ``ON`` references the padded
side; a comma or ``CROSS`` join whose equality lives in ``WHERE`` rather than
``ON``; ``USING``/``NATURAL JOIN``, which sqlglot gives no ``ON`` for either;
``EXISTS`` and ``IN (subquery)`` spellings of a semi join; and unqualified ``ON``
columns. An ``ON`` column read through a CTE or derived table resolves to the
column it copies unchanged (see :func:`copy_origin`), so import CTEs and
pass-through subqueries are seen; a CTE that computes the key, or a union, is not.
In the other direction, a filtered CTE over the parent still resolves to the
parent's own column, so the finding fires even though the CTE's filter, not only
a missing parent row, can drop a child whose foreign key genuinely holds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceKind
from dblect.lineage.property import copied_column, resolved_column_ref
from dblect.sql import _sqlglot as sg
from dblect.sql import anti_join
from dblect.types.bridge import ForeignKeyEdge

UnguardedEdges = Mapping[tuple[ColumnRef, ColumnRef], ForeignKeyEdge]
"""Declared edges with no covering test, keyed ``(child, parent)``."""


@dataclass(frozen=True, slots=True)
class OrphanDropSite:
    """One join whose row effect drops the unmatched side of a declared,
    unguarded foreign key.

    ``narrowed`` is true when the ``ON`` clause carries conjuncts beyond the one
    that matched the edge (a second key column, a literal pin): the join then
    drops a superset of the orphans, so a message must not imply the foreign key
    is the only way a row leaves.
    """

    join: exp.Join
    edge: ForeignKeyEdge
    narrowed: bool


def copy_origin(graph: ColumnLineageGraph) -> Callable[[exp.Column], ColumnRef | None]:
    """A ``ref_of`` that follows a column read through CTEs and derived tables to
    the model or source column it copies unchanged.

    Steps with :func:`copied_column` while the ref is a CTE-kind ref; anything
    computed, a union, or a cycle yields ``None``, so the join stays silent.
    """

    def ref_of(col: exp.Column) -> ColumnRef | None:
        ref = resolved_column_ref(col)
        seen: set[ColumnRef] = set()
        while ref is not None and ref.source.kind is SourceKind.CTE:
            if ref in seen:
                return None
            seen.add(ref)
            derivation = graph.derivation(ref)
            ref = None if derivation is None else copied_column(derivation)
        return ref

    return ref_of


def orphan_drop_sites(
    tree: Expr,
    edges: UnguardedEdges,
    ref_of: Callable[[exp.Column], ColumnRef | None],
) -> list[OrphanDropSite]:
    """Every join in ``tree`` whose row effect drops a declared, unguarded foreign
    key's unmatched child rows.

    Walks every ``SELECT`` in ``tree``, including one nested in a CTE body. Per
    join: the ``LEFT JOIN ... IS NULL`` anti-join idiom is skipped (it surfaces
    orphans on purpose); a join with no ``ON`` contributes nothing; each
    conjunctive column-to-column equality is checked against ``edges`` in both
    orientations; and a match fires only when the edge's child alias is among the
    join's own ``dropped_unmatched`` aliases (:func:`~dblect.sql._sqlglot.join_row_effects`),
    compared case-insensitively. An equality under ``OR`` is outside this fragment
    and silently skipped. One finding per ``(join, edge)``, however many equalities
    restate it.
    """
    out: list[OrphanDropSite] = []
    for sel in sg.find_all_selects(tree):
        anti_arms = anti_join.anti_arm_ids(sel)
        for effect in sg.join_row_effects(sel):
            if id(effect.join) in anti_arms:
                continue
            on = sg.on_of(effect.join)
            if on is None:
                continue
            dropped = {alias.lower() for alias in effect.dropped_unmatched}
            narrowed = len(sg.conjunctive_leaves(on)) > 1
            found: set[tuple[ColumnRef, ColumnRef]] = set()
            for left, right in sg.equality_column_pairs(on):
                left_ref = ref_of(left)
                right_ref = ref_of(right)
                if left_ref is None or right_ref is None:
                    continue
                for child_col, key in (
                    (left, (left_ref, right_ref)),
                    (right, (right_ref, left_ref)),
                ):
                    edge = edges.get(key)
                    child_alias = sg.column_table(child_col)
                    if (
                        edge is None
                        or key in found
                        or child_alias is None
                        or child_alias.lower() not in dropped
                    ):
                        continue
                    found.add(key)
                    out.append(OrphanDropSite(join=effect.join, edge=edge, narrowed=narrowed))
    return out
