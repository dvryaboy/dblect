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
