"""Typed accessors over sqlglot's AST.

sqlglot's `Expr.args` is typed as ``dict[str, Any]`` and several attributes on
expression nodes (``side``, ``kind``, ``alias_or_name``) are not narrowed in
the upstream stubs. We pay the conversion cost once here so the rest of
``dblect.sql`` reads as if sqlglot were strictly typed.

The casts are safe by construction: ``Select.args["joins"]`` is always a
``list[Join]`` when present, ``Select.args["from_"]`` is always a ``From``,
and so on (the keys are sqlglot's own naming conventions). Each helper
documents its key and what shape it returns.
"""

from __future__ import annotations

from enum import StrEnum
from typing import cast

import sqlglot.expressions as exp
from sqlglot import Expr


class JoinSide(StrEnum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"
    CROSS = "cross"


def from_of(sel: exp.Select) -> exp.From | None:
    """The ``FROM`` clause of a ``SELECT``, or ``None`` if absent.

    sqlglot 30+ keys the arg ``"from_"``; older 25.x kept it as ``"from"``.
    We try both so the static-analysis layer doesn't pin a minor version.
    """
    return cast("exp.From | None", sel.args.get("from_") or sel.args.get("from"))


def where_of(sel: exp.Select) -> exp.Where | None:
    return cast("exp.Where | None", sel.args.get("where"))


def joins_of(sel: exp.Select) -> list[exp.Join]:
    return cast("list[exp.Join]", sel.args.get("joins") or [])


def group_of(sel: exp.Select) -> exp.Group | None:
    return cast("exp.Group | None", sel.args.get("group"))


def on_of(j: exp.Join) -> Expr | None:
    return cast("Expr | None", j.args.get("on"))


def order_of(w: exp.Window) -> exp.Order | None:
    return cast("exp.Order | None", w.args.get("order"))


def partition_of(w: exp.Window) -> list[Expr]:
    return cast("list[Expr]", w.args.get("partition_by") or [])


def fn_of(w: exp.Window) -> Expr | None:
    return cast("Expr | None", w.this)


def join_side_of(j: exp.Join) -> JoinSide:
    """The side of `j` as a `JoinSide`.

    Reads sqlglot's ``side`` and ``kind`` token strings. ``CROSS`` overrides
    side (sqlglot keeps it on ``kind``); a missing/empty side defaults to
    ``INNER``, which is the SQL default for ``a JOIN b`` without a qualifier.
    """
    side = (j.side or "").upper()
    kind = (j.kind or "").upper()
    if "CROSS" in kind:
        return JoinSide.CROSS
    match side:
        case "LEFT":
            return JoinSide.LEFT
        case "RIGHT":
            return JoinSide.RIGHT
        case "FULL":
            return JoinSide.FULL
        case _:
            return JoinSide.INNER


def name_of(e: Expr) -> str:
    """``alias_or_name`` is the alias when there is one, the table/column name otherwise."""
    return e.alias_or_name


def column_table(c: exp.Column) -> str | None:
    """The qualifier on a column reference (``a`` in ``a.id``), or ``None``."""
    return c.table or None


def column_name(c: exp.Column) -> str:
    return c.name


def find_columns(e: Expr) -> list[exp.Column]:
    return list(e.find_all(exp.Column))


def find_all_selects(e: Expr) -> list[exp.Select]:
    return list(e.find_all(exp.Select))


def find_all_coalesce(e: Expr) -> list[exp.Coalesce]:
    return list(e.find_all(exp.Coalesce))


def find_all_windows(e: Expr) -> list[exp.Window]:
    return list(e.find_all(exp.Window))


def find_all_aggfunc(e: Expr) -> list[Expr]:
    return cast("list[Expr]", list(e.find_all(exp.AggFunc)))


def render_sql(e: Expr) -> str:
    return e.sql()


def equality_cols_on_alias(predicate: Expr, alias: str) -> frozenset[str] | None:
    """Columns on `alias` appearing in conjunctive equalities in `predicate`.

    Walks the AND-conjunction of `predicate`; for each leaf, accepts only
    ``exp.EQ`` between two bare columns where exactly one column's qualifier
    equals `alias`. Returns the set of column names on the `alias` side.

    Returns ``None`` if `predicate` contains anything other than a conjunction
    of such equalities (a disjunction, a function call, a range comparison,
    or an equality whose alias mix is ambiguous). Callers treat ``None`` as
    "can't simplify to a clean join-key" and conservatively skip.
    """
    cols: set[str] = set()
    for leaf in _conjunctive_leaves(predicate):
        if not isinstance(leaf, exp.EQ):
            return None
        left = leaf.this
        right = leaf.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            return None
        left_alias = column_table(left)
        right_alias = column_table(right)
        on_alias = [c for c, t in ((left, left_alias), (right, right_alias)) if t == alias]
        off_alias = [c for c, t in ((left, left_alias), (right, right_alias)) if t != alias]
        if len(on_alias) != 1 or len(off_alias) != 1:
            return None
        cols.add(column_name(on_alias[0]))
    return frozenset(cols)


def equality_literal_columns(predicate: Expr) -> tuple[exp.Column, ...]:
    """Columns a conjunct of `predicate` pins to a literal (``col = 'usd'``).

    Walks the AND-conjunction; a leaf contributes its column only when it is an
    ``exp.EQ`` between a bare column and a literal, in either order. Other leaves
    are simply skipped (unlike :func:`equality_cols_on_alias`, a non-equality
    conjunct does not poison the rest: each pin stands on its own conjunct).
    """
    out: list[exp.Column] = []
    for leaf in _conjunctive_leaves(predicate):
        if not isinstance(leaf, exp.EQ):
            continue
        sides = (leaf.this, leaf.expression)
        for col, lit in (sides, sides[::-1]):
            if (
                isinstance(col, exp.Column)
                and not isinstance(col.this, exp.Star)
                and isinstance(lit, exp.Literal)
            ):
                out.append(col)
                break
    return tuple(out)


def equality_column_pairs(predicate: Expr) -> tuple[tuple[exp.Column, exp.Column], ...]:
    """Column-to-column equalities in `predicate` (``a.x = b.y``), as ordered pairs.

    Walks the AND-conjunction; a leaf contributes a pair only when it is an ``exp.EQ``
    between two bare columns. This is the join-key extraction a join ON predicate needs
    (each equated pair, both sides resolved), companion to :func:`equality_literal_columns`
    for the literal-pin case. Non-equality and non-column leaves are skipped, each pair
    standing on its own conjunct."""
    out: list[tuple[exp.Column, exp.Column]] = []
    for leaf in _conjunctive_leaves(predicate):
        if not isinstance(leaf, exp.EQ):
            continue
        left, right = leaf.this, leaf.expression
        if (
            isinstance(left, exp.Column)
            and isinstance(right, exp.Column)
            and not isinstance(left.this, exp.Star)
            and not isinstance(right.this, exp.Star)
        ):
            out.append((left, right))
    return tuple(out)


def _conjunctive_leaves(predicate: Expr) -> list[Expr]:
    """Flatten an ``AND``-only conjunction into its leaves; non-AND nodes are leaves."""
    if isinstance(predicate, exp.And):
        return [*_conjunctive_leaves(predicate.this), *_conjunctive_leaves(predicate.expression)]
    return [predicate]


def line_range(e: Expr) -> tuple[int, int] | None:
    """The 1-indexed (start, end) source-line span covered by `e`.

    sqlglot only stamps token-position metadata onto ``Identifier`` nodes, so
    we walk descendants and take min/max over their `meta["line"]`. Returns
    `None` if no identifier carries a usable line number (rare; some literal-
    only expressions like ``select 1`` have no identifier children).

    Line numbers refer to the SQL the parser saw (the model's
    ``compiled_code``).
    """
    lines: list[int] = []
    for ident in e.find_all(exp.Identifier):
        line = ident.meta.get("line") if ident.meta else None
        if isinstance(line, int):
            lines.append(line)
    if not lines:
        return None
    return min(lines), max(lines)
