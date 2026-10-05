"""Dependent key without owner: a GROUP BY or a join that uses a ``unique_per`` column
without its owners.

``line_no.unique_per(order_id)`` says a line number means something only inside its
order. Grouping or joining on ``line_no`` alone treats lines of different orders as the
same entity, and the numbers come out plausible and wrong. Using the owners without the
dependent is a normal rollup or parent join and never fires.

When one GROUP BY, or the ON equalities of one join, use the dependent column, every owner
must be covered: used as a bare column, pinned to a literal for that table alias (by the
select's WHERE, by an ON conjunct that really filters that side of its join, or by a CTE or
derived table it reads), equated through the select's column-to-column equalities to a
covered column, equated to a column of the other side of the join in WHERE, or determined
by a covered column under the relation's functional dependencies. Columns are read through
renames, CTEs and derived tables to the relation whose column they copy. A computed GROUP BY
key covers nothing, since it is not known to be injective.

This is a direct structural read over the stamped tree, like the orphan-drop check, and
the pure signal is :func:`owned_key_sites`.

Not handled, by design:

* a relation reached only through another that shares its column (a keyless refunds table
  grouped by ``line_no`` when only ``order_lines`` declares the fact);
* ``USING`` and ``NATURAL`` joins (sqlglot gives no ON), comma joins whose equality lives
  in WHERE, and ``ROLLUP``/``CUBE``/``GROUPING SETS``/``GROUP BY ALL``;
* a pin inside a CTE or derived table counts for every column its body copies from that
  relation, so a self join in the body that pins one alias reads as pinning both; a pin in
  an upstream model is not seen.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

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


class _Slot(NamedTuple):
    """A column as one select reads it: the table alias it is read through (``None`` for an
    unqualified column in a select with several sources) and the origin column it copies."""

    alias: str | None
    ref: ColumnRef

    def on(self, alias: str | None, relation: SourceRef) -> bool:
        """Whether this is a column of ``relation`` read through ``alias``; an unqualified
        slot matches any alias."""
        return self.ref.source == relation and (
            self.alias is None or alias is None or self.alias == alias
        )


@dataclass(frozen=True, slots=True)
class _Scope:
    """What one SELECT lets a clause treat as already fixed: literal pins from its own
    WHERE and filtering ON conjuncts and from the sources it reads, the directed
    equalities that carry fixedness from one column to another, and its WHERE equalities
    (which cover an owner of one side of a join by equating it to the other side)."""

    select: exp.Select
    pins: frozenset[_Slot]
    carries: tuple[tuple[_Slot, _Slot], ...]
    where_pairs: tuple[tuple[_Slot, _Slot], ...]
    aliases: tuple[str, ...]


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
    pins_by_select = _pins_by_select(tree, ref_of)
    sites: list[OwnedKeySite] = []
    for sel in sg.find_all_selects(tree):
        scope = _scope(sel, pins_by_select.get(id(sel), frozenset()), ref_of)
        for index, join in enumerate(sg.joins_of(sel)):
            on = sg.on_of(join)
            if on is None:
                continue
            opposite = _opposite_aliases(scope.aliases, index)
            for alias, relation, used in _join_uses(sel, on, ref_of):
                sites.extend(
                    _judge(
                        scope,
                        by_relation,
                        fds_of,
                        join,
                        KeyedClause.JOIN,
                        alias,
                        relation,
                        used,
                        opposite.get(alias, frozenset()),
                        frozenset(),
                    )
                )
        group = sg.group_of(sel)
        if group is None or _unmodelled_grouping(group):
            continue
        used_by_alias: dict[tuple[str | None, SourceRef], set[str]] = defaultdict(set)
        grouped = _group_columns(sel, ref_of)
        for slot in grouped:
            used_by_alias[(slot.alias, slot.ref.source)].add(slot.ref.column)
        for (alias, relation), used in used_by_alias.items():
            sites.extend(
                _judge(
                    scope,
                    by_relation,
                    fds_of,
                    group,
                    KeyedClause.GROUP_BY,
                    alias,
                    relation,
                    frozenset(used),
                    frozenset(),
                    grouped,
                )
            )
    return sites


def _judge(
    scope: _Scope,
    owned_by_relation: Mapping[SourceRef, list[OwnedColumn]],
    fds_of: Callable[[SourceRef], FDSet],
    node: Expr,
    clause: KeyedClause,
    alias: str | None,
    relation: SourceRef,
    used: frozenset[str],
    opposite: frozenset[str],
    grouped: frozenset[_Slot],
) -> list[OwnedKeySite]:
    owned = owned_by_relation.get(relation, [])
    if not owned:
        return []
    used = frozenset(col.lower() for col in used)
    base = {*scope.pins, *grouped, *(_Slot(alias, ColumnRef(relation, col)) for col in used)}
    for left, right in scope.where_pairs:
        for mine, theirs in ((left, right), (right, left)):
            if mine.on(alias, relation) and theirs.alias in opposite:
                base.add(mine)
    reached = frozenset(
        slot.ref.column.lower() for slot in _close(base, scope.carries) if slot.on(alias, relation)
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


def _close(base: set[_Slot], carries: tuple[tuple[_Slot, _Slot], ...]) -> set[_Slot]:
    """``base`` closed under the directed equalities: a column a fixed one is carried to
    is fixed."""
    fixed = set(base)
    grew = True
    while grew:
        grew = False
        for source, target in carries:
            if source in fixed and target not in fixed:
                fixed.add(target)
                grew = True
    return fixed


def _source_aliases(sel: exp.Select) -> tuple[str, ...]:
    """The FROM table and each join's table, by lowercased alias, in order."""
    from_ = sg.from_of(sel)
    heads = [from_.this] if from_ is not None and from_.this is not None else []
    return tuple(sg.name_of(e).lower() for e in (*heads, *(j.this for j in sg.joins_of(sel))))


def _opposite_aliases(aliases: tuple[str, ...], join_index: int) -> dict[str, frozenset[str]]:
    """For join number ``join_index``, each alias to the aliases on the other side of that
    join: the joined table against everything before it, and each earlier one against
    the joined table. A later alias has no opposite."""
    position = join_index + 1
    joined = aliases[position]
    before = frozenset(aliases[:position])
    return {**dict.fromkeys(before, frozenset({joined})), joined: before}


def _alias_of(col: exp.Column, aliases: tuple[str, ...]) -> str | None:
    """The alias ``col`` is read through: its qualifier, or the only source's alias."""
    qualifier = sg.column_table(col)
    if qualifier is not None:
        return qualifier.lower()
    return aliases[0] if len(aliases) == 1 else None


def _slot(col: exp.Column, aliases: tuple[str, ...], ref_of: RefOf) -> _Slot | None:
    ref = ref_of(col)
    return None if ref is None else _Slot(_alias_of(col, aliases), ref)


def _scope(sel: exp.Select, pins: frozenset[_Slot], ref_of: RefOf) -> _Scope:
    carries, where_pairs = _equalities(sel, ref_of)
    return _Scope(sel, pins, carries, where_pairs, _source_aliases(sel))


def _restricts(effect: sg.JoinRowEffect, alias: str | None) -> bool:
    """Whether the join's ON keeps only matching rows of ``alias``. The join's own row
    effect decides: an INNER join restricts every alias, a LEFT join only its right side, a
    RIGHT join the aliases to its left, and a FULL join none. A column with no known alias
    is restricted only by an INNER join."""
    if effect.side is sg.JoinSide.INNER:
        return True
    return alias is not None and alias in {a.lower() for a in effect.dropped_unmatched}


def _filtering_predicates(sel: exp.Select) -> list[tuple[Expr, sg.JoinRowEffect | None]]:
    """Each predicate of ``sel`` with the join effect that decides which rows it filters;
    ``None`` for the WHERE, which filters whatever the joins produced."""
    where = sg.where_of(sel)
    out: list[tuple[Expr, sg.JoinRowEffect | None]] = (
        [(where.this, None)] if where is not None else []
    )
    for effect in sg.join_row_effects(sel):
        on = sg.on_of(effect.join)
        if on is not None:
            out.append((on, effect))
    return out


def _own_pins(sel: exp.Select, ref_of: RefOf) -> frozenset[_Slot]:
    """The columns a WHERE or an ON pins to a literal. Every conjunct of a join's ON holds
    for the matched rows, so the ON's own pins are closed under its equalities and kept
    for the aliases the join restricts: a pin on the preserved side of a LEFT join fixes
    the right side's rows, and a pin on the right side alone fixes nothing on the left."""
    aliases = _source_aliases(sel)
    pins: set[_Slot] = set()
    for predicate, effect in _filtering_predicates(sel):
        found = {
            slot
            for col in sg.equality_literal_columns(predicate)
            if (slot := _slot(col, aliases, ref_of)) is not None
        }
        if effect is None:
            pins.update(found)
            continue
        both_ways = [
            pair
            for left, right in sg.equality_column_pairs(predicate)
            if (a := _slot(left, aliases, ref_of)) is not None
            and (b := _slot(right, aliases, ref_of)) is not None
            for pair in ((a, b), (b, a))
        ]
        pins.update(
            slot for slot in _close(found, tuple(both_ways)) if _restricts(effect, slot.alias)
        )
    return frozenset(pins)


def _equalities(
    sel: exp.Select, ref_of: RefOf
) -> tuple[tuple[tuple[_Slot, _Slot], ...], tuple[tuple[_Slot, _Slot], ...]]:
    """The directed carries and the WHERE pairs. An equality ``u = v`` carries fixedness
    from ``u`` to ``v`` when the predicate restricts ``v``'s rows, so the null-supplying
    side of an outer join never fixes the preserved side."""
    aliases = _source_aliases(sel)
    carries: list[tuple[_Slot, _Slot]] = []
    where_pairs: list[tuple[_Slot, _Slot]] = []
    for predicate, effect in _filtering_predicates(sel):
        for left, right in sg.equality_column_pairs(predicate):
            left_slot, right_slot = _slot(left, aliases, ref_of), _slot(right, aliases, ref_of)
            if left_slot is None or right_slot is None:
                continue
            if effect is None:
                where_pairs.append((left_slot, right_slot))
            for source, target in ((left_slot, right_slot), (right_slot, left_slot)):
                if effect is None or _restricts(effect, target.alias):
                    carries.append((source, target))
    return tuple(carries), tuple(where_pairs)


def _pins_by_select(tree: Expr, ref_of: RefOf) -> dict[int, frozenset[_Slot]]:
    """For every select in ``tree`` (by ``id``), the pins its clauses may rely on: its own,
    plus those of each CTE or derived table it reads, lifted onto the alias that reads
    it. A union carries only the pins every arm has. A sibling CTE or another union arm
    contributes nothing."""
    out: dict[int, frozenset[_Slot]] = {}
    _node_pins(tree, {}, ref_of, out)
    for sel in sg.find_all_selects(tree):
        if id(sel) not in out:
            _select_pins(sel, {}, ref_of, out)
    return out


def _node_pins(
    node: Expr,
    ctes: Mapping[str, frozenset[ColumnRef]],
    ref_of: RefOf,
    out: dict[int, frozenset[_Slot]],
) -> frozenset[ColumnRef]:
    """The origin columns every row ``node`` yields has pinned to a literal."""
    node = node.unnest()
    if isinstance(node, exp.Select):
        return frozenset(slot.ref for slot in _select_pins(node, ctes, ref_of, out))
    if isinstance(node, exp.Union):
        arms = sg.union_arms(node)
        if not arms:
            return frozenset()
        per_arm = [_node_pins(arm, ctes, ref_of, out) for arm in arms]
        return per_arm[0].intersection(*per_arm[1:])
    return frozenset()


def _select_pins(
    sel: exp.Select,
    ctes: Mapping[str, frozenset[ColumnRef]],
    ref_of: RefOf,
    out: dict[int, frozenset[_Slot]],
) -> frozenset[_Slot]:
    local = sg.with_scope(sel, ctes, lambda body, scope: _node_pins(body, scope, ref_of, out))
    pins = set(_own_pins(sel, ref_of))
    from_ = sg.from_of(sel)
    sources = [from_.this] if from_ is not None and from_.this is not None else []
    for source in (*sources, *(j.this for j in sg.joins_of(sel))):
        lifted: frozenset[ColumnRef] = frozenset()
        if isinstance(source, exp.Subquery):
            lifted = _node_pins(source, local, ref_of, out)
        elif isinstance(source, exp.Table) and not source.db:
            lifted = local.get(source.name, frozenset())
        alias = sg.name_of(source).lower()
        pins.update(_Slot(alias, ref) for ref in lifted)
    result = frozenset(pins)
    out[id(sel)] = result
    for nested in sel.find_all(exp.Select):
        if nested is not sel and id(nested) not in out:
            _select_pins(nested, local, ref_of, out)
    return result


def _join_uses(
    sel: exp.Select, on: Expr, ref_of: RefOf
) -> list[tuple[str, SourceRef, frozenset[str]]]:
    """Per table alias, the origin columns the ON equates to another column,
    grouped by the relation they resolve to. Two aliases of one relation are judged
    separately, so a self join on the full key through one alias and part of it through
    the other still fires for the part."""
    aliases = _source_aliases(sel)
    uses: dict[tuple[str, SourceRef], set[str]] = defaultdict(set)
    for left, right in sg.equality_column_pairs(on):
        for col in (left, right):
            slot = _slot(col, aliases, ref_of)
            if slot is not None and slot.alias is not None:
                uses[(slot.alias, slot.ref.source)].add(slot.ref.column)
    return [(alias, relation, frozenset(cols)) for (alias, relation), cols in uses.items()]


def _group_columns(sel: exp.Select, ref_of: RefOf) -> frozenset[_Slot]:
    """The bare grouped columns. A computed key (``floor(order_id / 100)``, a ``CASE``)
    merges rows that differ in the columns inside it and is not known to be injective, so
    it neither counts as a use nor fixes any column. A renaming alias (``line_no as ln ...
    group by ln``) names no input column, so its target reads through to the projection."""
    aliases = _source_aliases(sel)
    grouped: set[_Slot] = set()
    for target in sg.group_targets(sel):
        for e in (target.grounded_expression, target.expression):
            if isinstance(e, exp.Column) and (slot := _slot(e, aliases, ref_of)) is not None:
                grouped.add(slot)
                break
    return frozenset(grouped)


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
