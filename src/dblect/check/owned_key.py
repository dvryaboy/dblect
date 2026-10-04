"""Dependent key without owner: a GROUP BY or a join that uses a ``unique_per`` column
without its owners.

``line_no.unique_per(order_id)`` says a line number means something only inside its
order. Grouping or joining on ``line_no`` alone treats lines of different orders as the
same entity, and the numbers come out plausible and wrong. Using the owners without the
dependent is a normal rollup or parent join and never fires.

When one GROUP BY, or the ON equalities of one join, use the dependent column, every owner
must be covered: used, pinned to a literal anywhere in the model (a WHERE, a join-side
filter, a CTE body), equated through the select's own column-to-column equalities to a
covered column, or determined by a covered column under the relation's functional
dependencies. Columns are read through renames, CTEs and derived tables to the relation
whose column they copy.

This is a direct structural read over the stamped tree, like the orphan-drop check, and
the pure signal is :func:`owned_key_sites`.

Not handled, by design:

* a relation reached only through another that shares its column (a keyless refunds table
  grouped by ``line_no`` when only ``order_lines`` declares the fact);
* ``USING`` and ``NATURAL`` joins (sqlglot gives no ON), comma joins whose equality lives
  in WHERE, and ``ROLLUP``/``CUBE``/``GROUPING SETS``/``GROUP BY ALL``;
* a literal pin counts model-wide, so a filter in an unrelated CTE over the same relation
  silences the finding; a pin in an upstream model is not seen.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.properties.functional_dependency import FDSet, determines
from dblect.sql import _sqlglot as sg
from dblect.types import OwnedColumn

RefOf = Callable[[exp.Column], ColumnRef | None]


class KeyedClause(StrEnum):
    GROUP_BY = "GROUP BY"
    JOIN = "JOIN"


@dataclass(frozen=True, slots=True)
class OwnedKeySite:
    """One clause that uses ``column`` of ``relation`` without covering its owners.
    ``missing`` are the owners it leaves unpinned and undetermined. ``node`` is the
    ``Group`` or ``Join`` it sits on and ``select`` the statement holding it, the line
    fallback for a node sqlglot gave none."""

    node: Expr
    select: exp.Select
    clause: KeyedClause
    relation: SourceRef
    column: str
    missing: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Scope:
    """What one SELECT lets a clause treat as already fixed: literal pins from anywhere
    in the model, and the select's own column-to-column equalities."""

    select: exp.Select
    pins: frozenset[ColumnRef]
    equalities: tuple[tuple[ColumnRef, ColumnRef], ...]


def owned_key_sites(
    tree: Expr,
    owned: tuple[OwnedColumn, ...],
    fds_of: Callable[[SourceRef], FDSet],
    ref_of: RefOf,
) -> list[OwnedKeySite]:
    """Every GROUP BY and every join ON in ``tree`` that uses an owned column of one
    relation while some of its owners stay uncovered.

    Per join, the used columns are those of one table alias in the ON's top-level
    column-to-column equalities (an equality under ``OR`` is outside the fragment). Per
    GROUP BY, they are the bare grouped columns, read through CTEs and derived tables to
    the relation they copy. One site per (clause, relation, alias, owned column).
    """
    by_relation: dict[SourceRef, list[OwnedColumn]] = defaultdict(list)
    for entry in owned:
        by_relation[entry.scope].append(entry)
    pins = _literal_pins(tree, ref_of)
    sites: list[OwnedKeySite] = []
    for sel in sg.find_all_selects(tree):
        scope = _Scope(sel, pins, _equalities(sel, ref_of))
        for join in sg.joins_of(sel):
            on = sg.on_of(join)
            if on is None:
                continue
            for relation, used in _join_uses(on, ref_of):
                sites.extend(
                    _judge(scope, by_relation, fds_of, join, KeyedClause.JOIN, relation, used, ())
                )
        group = sg.group_of(sel)
        if group is None or _unmodelled_grouping(group):
            continue
        grouped, inside = _group_columns(sel, ref_of)
        used_by_relation: dict[SourceRef, set[str]] = defaultdict(set)
        for ref in grouped:
            used_by_relation[ref.source].add(ref.column)
        for relation, used in used_by_relation.items():
            sites.extend(
                _judge(
                    scope,
                    by_relation,
                    fds_of,
                    group,
                    KeyedClause.GROUP_BY,
                    relation,
                    frozenset(used),
                    (*grouped, *inside),
                )
            )
    return sites


def _judge(
    scope: _Scope,
    owned_by_relation: Mapping[SourceRef, list[OwnedColumn]],
    fds_of: Callable[[SourceRef], FDSet],
    node: Expr,
    clause: KeyedClause,
    relation: SourceRef,
    used: frozenset[str],
    also_fixed: tuple[ColumnRef, ...],
) -> list[OwnedKeySite]:
    owned = owned_by_relation.get(relation, [])
    if not owned:
        return []
    used = frozenset(col.lower() for col in used)
    base = {*scope.pins, *also_fixed, *(ColumnRef(relation, col) for col in used)}
    reached = frozenset(
        ref.column.lower() for ref in _close(base, scope.equalities) if ref.source == relation
    )
    fds = fds_of(relation)
    anchor = node if sg.line_range(node) is not None else scope.select
    sites: list[OwnedKeySite] = []
    for entry in owned:
        if entry.column not in used:
            continue
        missing = frozenset(o for o in entry.owners if not determines(fds, reached, o))
        if missing:
            sites.append(
                OwnedKeySite(anchor, scope.select, clause, relation, entry.column, missing)
            )
    return sites


def _close(
    base: set[ColumnRef], equalities: tuple[tuple[ColumnRef, ColumnRef], ...]
) -> set[ColumnRef]:
    """``base`` closed under the equalities: a column equated to a fixed one is fixed."""
    fixed = set(base)
    grew = True
    while grew:
        grew = False
        for left, right in equalities:
            if (left in fixed) != (right in fixed):
                fixed.update((left, right))
                grew = True
    return fixed


def _literal_pins(tree: Expr, ref_of: RefOf) -> frozenset[ColumnRef]:
    """Origin columns equated to a literal in any WHERE or ON of the model."""
    pins: set[ColumnRef] = set()
    for sel in sg.find_all_selects(tree):
        for predicate in _predicates(sel):
            for col in sg.equality_literal_columns(predicate):
                ref = ref_of(col)
                if ref is not None:
                    pins.add(ref)
    return frozenset(pins)


def _equalities(sel: exp.Select, ref_of: RefOf) -> tuple[tuple[ColumnRef, ColumnRef], ...]:
    pairs: list[tuple[ColumnRef, ColumnRef]] = []
    for predicate in _predicates(sel):
        for left, right in sg.equality_column_pairs(predicate):
            left_ref, right_ref = ref_of(left), ref_of(right)
            if left_ref is not None and right_ref is not None:
                pairs.append((left_ref, right_ref))
    return tuple(pairs)


def _predicates(sel: exp.Select) -> list[Expr]:
    where = sg.where_of(sel)
    candidates = [where.this if where is not None else None, *map(sg.on_of, sg.joins_of(sel))]
    return [p for p in candidates if p is not None]


def _join_uses(on: Expr, ref_of: RefOf) -> list[tuple[SourceRef, frozenset[str]]]:
    """Per table alias, the origin columns the ON equates to another column,
    grouped by the relation they resolve to. Two aliases of one relation are judged
    separately, so a self join on the full key through one alias and part of it through
    the other still fires for the part."""
    uses: dict[tuple[str | None, SourceRef], set[str]] = defaultdict(set)
    for left, right in sg.equality_column_pairs(on):
        for col in (left, right):
            ref = ref_of(col)
            if ref is not None:
                uses[(_alias(col), ref.source)].add(ref.column)
    return [(relation, frozenset(cols)) for (_, relation), cols in uses.items()]


def _alias(col: exp.Column) -> str | None:
    alias = sg.column_table(col)
    return alias.lower() if alias is not None else None


def _group_columns(
    sel: exp.Select, ref_of: RefOf
) -> tuple[frozenset[ColumnRef], frozenset[ColumnRef]]:
    """The bare grouped columns, and the columns read inside any other grouped
    expression. The second set is treated as fixed without counting as a use, so a
    computed key never reads as a missing column. A renaming alias (``line_no as ln ...
    group by ln``) names no input column, so its target reads through to the projection."""
    grouped: set[ColumnRef] = set()
    inside: set[ColumnRef] = set()
    for target in sg.group_targets(sel):
        written = target.grounded_expression
        resolved = target.expression
        bare = next(
            (ref for e in (written, resolved) if isinstance(e, exp.Column) and (ref := ref_of(e))),
            None,
        )
        if bare is not None:
            grouped.add(bare)
            continue
        for e in (written, resolved):
            inside.update(ref for col in sg.find_columns(e) if (ref := ref_of(col)))
    return frozenset(grouped), frozenset(inside)


def _unmodelled_grouping(group: exp.Group) -> bool:
    return any(group.args.get(arg) for arg in ("rollup", "cube", "grouping_sets", "all", "totals"))


def owned_key_message(site: OwnedKeySite, relation_name: str) -> str:
    """The finding's wording: what the clause reads, which owners it leaves out, and the
    ways to settle it. Never claims the data is wrong, only that rows differing in the
    missing owners are treated as one."""
    missing = ", ".join(sorted(site.missing))
    action = "groups" if site.clause is KeyedClause.GROUP_BY else "matches"
    return (
        f"this {site.clause.value} {action} {relation_name!r} on {site.column}, which is "
        f"meaningful only within {missing}, without covering it. Rows that differ only in "
        f"{missing} are treated as one. Add {missing} to the clause, filter it to one value, "
        "or declare the dependency if one holds; add a noqa if the rollup is intended."
    )
