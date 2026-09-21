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

Left undetected, by design, rather than half-handled: a ``LEFT JOIN ... WHERE
parent.col = x`` that re-filters the padded side to a literal (turning the LEFT
into an effective INNER); a later join whose ``ON`` references the padded side;
a comma or ``CROSS`` join whose equality lives in ``WHERE`` rather than ``ON``;
and ``USING``/``NATURAL JOIN``, which sqlglot gives no ``ON`` for either, so
they read the same as a bare ``CROSS`` here. In the other direction, a join
whose parent side is a filtered CTE or subquery over the parent relation still
resolves to the parent relation's own column, so the finding fires with no
narrowing sentence even though the CTE's own filter, not only a missing parent
row, can drop a child whose foreign key genuinely holds.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.graph import ColumnRef
from dblect.sql import _sqlglot as sg
from dblect.sql import anti_join
from dblect.sql._sqlglot import JoinSide
from dblect.types.bridge import ForeignKeyEdge


@dataclass(frozen=True, slots=True)
class OrphanDropSite:
    """One join whose row effect drops the unmatched side of a declared,
    unguarded foreign key.

    ``edge`` carries the child/parent columns; the site does not restate them.
    ``narrowed`` is true when the ``ON`` clause carries conjuncts beyond the one
    that matched the edge (a second key column, a literal pin): the join then
    drops a superset of the orphans, so a message must not imply the foreign key
    is the only way a row leaves.
    """

    join: exp.Join
    side: JoinSide
    edge: ForeignKeyEdge
    narrowed: bool


EdgesByChild = Mapping[ColumnRef, tuple[ForeignKeyEdge, ...]]


def edges_by_child(edges: Iterable[ForeignKeyEdge]) -> dict[ColumnRef, tuple[ForeignKeyEdge, ...]]:
    """``edges`` indexed by child column: the shape :func:`orphan_drop_sites` reads
    to resolve a join's ``ON``-clause equality against a declared edge."""
    grouped: dict[ColumnRef, list[ForeignKeyEdge]] = {}
    for edge in edges:
        grouped.setdefault(edge.child, []).append(edge)
    return {child: tuple(es) for child, es in grouped.items()}


def unguarded_edges(
    edges: Iterable[ForeignKeyEdge], guarded: frozenset[tuple[ColumnRef, ColumnRef]]
) -> tuple[ForeignKeyEdge, ...]:
    """``edges`` whose ``(child, parent)`` pair is not already in ``guarded``
    (see ``relationship_tested_edges``): the edges an inner join can silently
    defeat with no other loud failure mode."""
    return tuple(edge for edge in edges if (edge.child, edge.parent) not in guarded)


def orphan_drop_sites(
    tree: Expr,
    edges_by_child: EdgesByChild,
    ref_of: Callable[[exp.Column], ColumnRef | None],
) -> list[OrphanDropSite]:
    """Every join in ``tree`` whose row effect drops a declared, unguarded foreign
    key's unmatched child rows.

    Walks every ``SELECT`` in ``tree``, including one nested in a CTE body: the
    builder stamps a resolved ``ColumnRef`` onto a column reference in any
    position, so a join buried in a CTE reads the same as one at the statement's
    top level. Per join: a join already recognised as the ``LEFT JOIN ...
    IS NULL`` anti-join idiom is skipped (that idiom surfaces orphans on purpose,
    not silently); a join with no ``ON`` (``CROSS``, ``NATURAL``, bare ``USING``)
    contributes nothing; each conjunctive column-to-column equality is checked
    against ``edges_by_child`` in both orientations, since an ``ON`` clause can
    write either side first; and a match fires only when the edge's child alias
    is among the join's own ``dropped_unmatched`` aliases (:func:`~dblect.sql._sqlglot.join_row_effects`).
    An equality under ``OR`` is outside this fragment (only a top-level
    conjunction decodes), so it is silently skipped rather than guessed at.
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
            leaves = sg.conjunctive_leaves(on)
            narrowed = len(leaves) > 1
            for left, right in sg.equality_column_pairs(on):
                left_ref = ref_of(left)
                right_ref = ref_of(right)
                if left_ref is None or right_ref is None:
                    continue
                match = _matching_edge(left, left_ref, right, right_ref, edges_by_child)
                if match is None:
                    continue
                edge, child_col = match
                child_alias = sg.column_table(child_col)
                if child_alias is None or child_alias not in effect.dropped_unmatched:
                    continue
                out.append(
                    OrphanDropSite(
                        join=effect.join,
                        side=effect.side,
                        edge=edge,
                        narrowed=narrowed,
                    )
                )
    return out


def _matching_edge(
    left: exp.Column,
    left_ref: ColumnRef,
    right: exp.Column,
    right_ref: ColumnRef,
    edges_by_child: EdgesByChild,
) -> tuple[ForeignKeyEdge, exp.Column] | None:
    """The declared edge the ``{left_ref, right_ref}`` equality realizes, paired
    with whichever raw column node sits on the edge's child side (its ``ON``-clause
    alias is what a caller needs, not the resolved, upstream-following ``ColumnRef``).
    Tried both orientations, since the child can be written on either side of ``=``.
    """
    for child_ref, child_col, parent_ref in (
        (left_ref, left, right_ref),
        (right_ref, right, left_ref),
    ):
        for edge in edges_by_child.get(child_ref, ()):
            if edge.parent == parent_ref:
                return edge, child_col
    return None
