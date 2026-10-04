"""Partial composite key: a GROUP BY or a join that reaches into a declared
composite key without covering it.

A column that is unique only together with another (a line number within an order, a
seat within a flight) is easy to group or join on alone, and the numbers come out
plausible and wrong. A relation with a declared key of two or more columns makes the
mistake checkable: when one GROUP BY, or the ON equalities of one join, use some of the
key's columns and the rest are neither used, pinned, nor determined by what is used,
rows that differ only in the missing columns are treated as the same entity.

A key column is covered when it is used, pinned to a literal anywhere in the model
(a WHERE, a join-side filter, a CTE body), equated through the select's own
column-to-column equalities to a covered column, or determined by a covered column under
the relation's functional dependencies. A clause that covers any declared key of the
relation, composite or not, is silent. A clause whose only used key columns are foreign
keys is silent too: rolling lines up to their order, or joining them to it, is what the
reference is for, and without that carve-out every parent-id join would fire.

This is a direct structural read over the stamped tree, like the orphan-drop check, and
the pure signal is :func:`partial_key_sites`.

Not handled, by design:

* a relation reached only through another that shares its column (a keyless refunds
  table grouped by ``line_no`` when only ``order_lines`` declares the key) needs the
  composite references of #283;
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
from dblect.lineage.properties.uniqueness import Key
from dblect.sql import _sqlglot as sg

KeysByRelation = Mapping[SourceRef, frozenset[Key]]
RefOf = Callable[[exp.Column], ColumnRef | None]


class KeyedClause(StrEnum):
    GROUP_BY = "GROUP BY"
    JOIN = "JOIN"


@dataclass(frozen=True, slots=True)
class PartialKeySite:
    """One clause whose used columns reach into ``key`` of ``relation`` without covering
    it. ``used`` are the key columns the clause reads, ``missing`` those it leaves
    unpinned and undetermined. ``node`` is the ``Group`` or ``Join`` it sits on and
    ``select`` the statement holding it, the line fallback for a node sqlglot gave none."""

    node: Expr
    select: exp.Select
    clause: KeyedClause
    relation: SourceRef
    key: Key
    used: frozenset[str]
    missing: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Declarations:
    keys: KeysByRelation
    fds_of: Callable[[SourceRef], FDSet]
    foreign_key_children: frozenset[ColumnRef]


@dataclass(frozen=True, slots=True)
class _Scope:
    """What one SELECT lets a clause treat as already fixed: literal pins from anywhere
    in the model, and the select's own column-to-column equalities."""

    select: exp.Select
    pins: frozenset[ColumnRef]
    equalities: tuple[tuple[ColumnRef, ColumnRef], ...]


def partial_key_sites(
    tree: Expr,
    keys: KeysByRelation,
    fds_of: Callable[[SourceRef], FDSet],
    foreign_key_children: frozenset[ColumnRef],
    ref_of: RefOf,
) -> list[PartialKeySite]:
    """Every GROUP BY and every join ON in ``tree`` that uses part of a declared
    composite key of one relation and covers neither that key nor any other key of it.

    Per join, the used columns are those of one table alias in the ON's top-level
    column-to-column equalities (an equality under ``OR`` is outside the fragment). Per
    GROUP BY, they are the bare grouped columns, read through CTEs and derived tables to
    the relation they copy. One site per (clause, relation, alias), naming the composite
    key with the fewest missing columns.
    """
    if not any(len(key) > 1 for bucket in keys.values() for key in bucket):
        return []
    declarations = _Declarations(keys, fds_of, foreign_key_children)
    pins = _literal_pins(tree, ref_of)
    sites: list[PartialKeySite | None] = []
    for sel in sg.find_all_selects(tree):
        scope = _Scope(sel, pins, _equalities(sel, ref_of))
        for join in sg.joins_of(sel):
            on = sg.on_of(join)
            if on is None:
                continue
            for relation, used in _join_uses(on, ref_of):
                sites.append(
                    _judge(scope, declarations, join, KeyedClause.JOIN, relation, used, ())
                )
        group = sg.group_of(sel)
        if group is None or _unmodelled_grouping(group):
            continue
        grouped, inside = _group_columns(sel, ref_of)
        by_relation: dict[SourceRef, set[str]] = defaultdict(set)
        for ref in grouped:
            by_relation[ref.source].add(ref.column)
        for relation, used in by_relation.items():
            sites.append(
                _judge(
                    scope,
                    declarations,
                    group,
                    KeyedClause.GROUP_BY,
                    relation,
                    frozenset(used),
                    (*grouped, *inside),
                )
            )
    return [site for site in sites if site is not None]


def _judge(
    scope: _Scope,
    declarations: _Declarations,
    node: Expr,
    clause: KeyedClause,
    relation: SourceRef,
    used: frozenset[str],
    also_fixed: tuple[ColumnRef, ...],
) -> PartialKeySite | None:
    declared = declarations.keys.get(relation, frozenset())
    if not declared:
        return None
    base = {*scope.pins, *also_fixed, *(ColumnRef(relation, col) for col in used)}
    reached = frozenset(
        ref.column for ref in _close(base, scope.equalities) if ref.source == relation
    )
    fds = declarations.fds_of(relation)

    def covered(col: str) -> bool:
        return determines(fds, reached, col)

    if any(all(covered(col) for col in key) for key in declared):
        return None
    fk = declarations.foreign_key_children
    candidates = [
        (key, frozenset(col for col in key if not covered(col)))
        for key in declared
        if len(key) > 1 and any(ColumnRef(relation, col) not in fk for col in used & key)
    ]
    if not candidates:
        return None
    key, missing = min(candidates, key=lambda c: (len(c[1]), sorted(c[0])))
    anchor = node if sg.line_range(node) is not None else scope.select
    return PartialKeySite(anchor, scope.select, clause, relation, key, used & key, missing)


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


def partial_key_message(site: PartialKeySite, relation_name: str) -> str:
    """The finding's wording: what the clause reads, which key columns it leaves out, and the
    ways to settle it. Never claims the data is wrong, only that rows differing in the missing
    columns are treated as one."""
    key = ", ".join(sorted(site.key))
    used = ", ".join(sorted(site.used))
    missing = ", ".join(sorted(site.missing))
    action = "groups" if site.clause is KeyedClause.GROUP_BY else "matches"
    return (
        f"this {site.clause.value} {action} {relation_name!r} on {used} but not the rest of its "
        f"declared key ({key}), missing {missing}. Rows that differ only in {missing} are treated "
        f"as one. Use the whole key, filter {missing} to one value, or declare the dependency if "
        "one holds; add a noqa if the subset is intended."
    )
