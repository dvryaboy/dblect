"""Lowering the AST to SQL, and running a predicate against data.

The internal AST is the source of truth; SQL is one target. :func:`compile_value`
renders a value expression to a sqlglot tree (a column, an arithmetic fold, an
aggregate call), :func:`compile_predicate` renders a row-level predicate, and
:func:`compile_aggregate_query` assembles the grouped ``SELECT`` an aggregate
denotes. Rendering through sqlglot keeps the dialect handling and quoting in one
well-exercised place.

:func:`evaluate_predicate` is the execution end: a comparison of two aggregates
(the conservation shape, ``a.sum() == b.sum()`` per group) compiled to a query per
side, run against generated rows in an in-memory DuckDB, and compared group by
group under the predicate's tolerance. This is the runtime-checked half of the
contract surface, the analyzer never reasons over it. Cross-relation joins
(``joined_on``) and the materialized-DataFrame escape hatch are not lowered here
yet; they are the natural next increment.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import duckdb
import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.contracts.ast import (
    Agg,
    AggFunc,
    Arith,
    ArithOp,
    Between,
    BoolNode,
    BoolOp,
    CmpOp,
    Col,
    Compare,
    InSet,
    IsNull,
    Lit,
    Pred,
    ValueExpr,
)
from dblect.contracts.proxy import ContractError

# How a model reference resolves to a SQL table name. ``None`` is the contract's
# own model; by default a value renders with the model name as the table qualifier
# and a bare column for ``self``.
TableOf = Callable[[str | None], str | None]


def _identity_table(model: str | None) -> str | None:
    return model


def compile_value(expr: ValueExpr, *, table_of: TableOf = _identity_table) -> Expr:
    """A value expression as a sqlglot tree. An aggregate renders as its function
    call (the ``SELECT`` item); grouping and the FROM live in the surrounding
    query, see :func:`compile_aggregate_query`."""
    if isinstance(expr, Col):
        table = table_of(expr.model)
        return exp.column(expr.name, table=table) if table else exp.column(expr.name)
    if isinstance(expr, Lit):
        return exp.Literal.number(_render_number(expr.value))
    if isinstance(expr, Arith):
        return _arith(expr, table_of)
    return _aggregate_call(expr, table_of)


_ARITH_NODE: Mapping[ArithOp, type[exp.Binary]] = {
    ArithOp.ADD: exp.Add,
    ArithOp.SUB: exp.Sub,
    ArithOp.MUL: exp.Mul,
    ArithOp.DIV: exp.Div,
}


def _arith(node: Arith, table_of: TableOf) -> Expr:
    # Parenthesize a nested arithmetic operand so the rendered SQL keeps the AST's
    # grouping: sqlglot prints by tree shape and will not re-derive the precedence
    # we already fixed, so ``Div(Add(a, b), 2)`` must read ``(a + b) / 2``.
    left = _operand(node.left, table_of)
    right = _operand(node.right, table_of)
    return _ARITH_NODE[node.op](this=left, expression=right)


def _operand(expr: ValueExpr, table_of: TableOf) -> Expr:
    compiled = compile_value(expr, table_of=table_of)
    return exp.paren(compiled) if isinstance(expr, Arith) else compiled


def _aggregate_call(node: Agg, table_of: TableOf) -> Expr:
    operand = compile_value(node.operand, table_of=table_of)
    if node.func is AggFunc.SUM:
        return exp.Sum(this=operand)
    if node.func is AggFunc.AVG:
        return exp.Avg(this=operand)
    if node.func is AggFunc.MIN:
        return exp.Min(this=operand)
    if node.func is AggFunc.MAX:
        return exp.Max(this=operand)
    if node.func is AggFunc.COUNT:
        return exp.Count(this=operand)
    return exp.Count(this=exp.Distinct(expressions=[operand]))


_CMP_NODE: Mapping[CmpOp, type[exp.Condition]] = {
    CmpOp.EQ: exp.EQ,
    CmpOp.NE: exp.NEQ,
    CmpOp.LT: exp.LT,
    CmpOp.LE: exp.LTE,
    CmpOp.GT: exp.GT,
    CmpOp.GE: exp.GTE,
}


def compile_predicate(pred: Pred, *, table_of: TableOf = _identity_table) -> Expr:
    """A row-level predicate as a sqlglot boolean. A :class:`Compare` of two
    aggregates is not a row predicate; that is the conservation shape
    :func:`evaluate_predicate` runs, and rendering it here raises."""
    if isinstance(pred, Compare):
        if _has_aggregate(pred.left) or _has_aggregate(pred.right):
            raise ContractError("an aggregate comparison is run, not rendered as a row predicate")
        left = compile_value(pred.left, table_of=table_of)
        right = compile_value(pred.right, table_of=table_of)
        return _CMP_NODE[pred.op](this=left, expression=right)
    if isinstance(pred, IsNull):
        is_null: Expr = exp.Is(
            this=compile_value(pred.column, table_of=table_of), expression=exp.Null()
        )
        return exp.Not(this=is_null) if pred.negated else is_null
    if isinstance(pred, InSet):
        column = compile_value(pred.column, table_of=table_of)
        return exp.In(this=column, expressions=[_literal(v) for v in pred.values])
    if isinstance(pred, Between):
        return exp.Between(
            this=compile_value(pred.column, table_of=table_of),
            low=exp.Literal.number(_render_number(pred.low)),
            high=exp.Literal.number(_render_number(pred.high)),
        )
    return _bool(pred, table_of)


def _bool(node: BoolNode, table_of: TableOf) -> Expr:
    parts = [compile_predicate(p, table_of=table_of) for p in node.operands]
    if node.op is BoolOp.NOT:
        return exp.Not(this=exp.paren(parts[0]))
    joiner = exp.and_ if node.op is BoolOp.AND else exp.or_
    return joiner(*parts)


def compile_aggregate_query(agg: Agg, table: str, *, value_alias: str = "value") -> exp.Select:
    """The grouped ``SELECT`` an aggregate denotes over one relation: the group
    keys, then the aggregate aliased ``value_alias``, ``FROM table`` grouped by the
    keys. ``joined_on`` (a measure grouped by another relation's key) is not lowered
    here yet."""
    if agg.joined_on is not None:
        raise ContractError("joined_on aggregates are not compiled to a single-relation query yet")
    table_of: TableOf = lambda _model: None  # noqa: E731 (one-line local resolver)
    keys = [compile_value(k, table_of=table_of) for k in agg.group_by]
    measure = exp.alias_(_aggregate_call(agg, table_of), value_alias)
    select = exp.select(*keys, measure).from_(table)
    if agg.group_by:
        select = select.group_by(*keys)
    return select


# --- running a conservation predicate -------------------------------------------


@dataclass(frozen=True, slots=True)
class GroupMismatch:
    """One group where the two sides of a conservation predicate disagreed."""

    key: tuple[Any, ...]
    left: float | None
    right: float | None


@dataclass(frozen=True, slots=True)
class GroupedResult:
    """The outcome of running a conservation predicate: whether every group held,
    and the groups that did not."""

    ok: bool
    mismatches: tuple[GroupMismatch, ...]


def evaluate_predicate(
    pred: Pred,
    tables: Mapping[str | None, Sequence[Mapping[str, Any]]],
    *,
    tolerance: float = 0.0,
) -> GroupedResult:
    """Run a comparison of two aggregates over ``tables`` and report per-group.

    ``tables`` maps a model reference (``None`` for the contract's own model) to its
    rows. Each side compiles to a grouped query, both run in one in-memory DuckDB,
    and the groups are aligned by their key tuple. The comparison and its tolerance
    come from the predicate; a non-comparison or a non-aggregate side is a contract
    error, since only the aggregate-comparison shape is runnable this way.
    """
    if not isinstance(pred, Compare):
        raise ContractError("only a comparison predicate can be run as a conservation check")
    left, right = pred.left, pred.right
    if not isinstance(left, Agg) or not isinstance(right, Agg):
        raise ContractError("a conservation check compares two aggregates")
    eps = pred.tolerance.eps if pred.tolerance is not None else tolerance
    reference = pred.tolerance.relative_to if pred.tolerance is not None else None

    with duckdb.connect(":memory:") as con:
        left_rows = _run_side(con, left, tables, alias="l")
        right_rows = _run_side(con, right, tables, alias="r")
        ref_rows = (
            _run_reference(con, reference, left.group_by, tables)
            if reference is not None
            else None
        )

    mismatches = _align(left_rows, right_rows, pred.op, eps, ref_rows)
    return GroupedResult(ok=not mismatches, mismatches=tuple(mismatches))


def _run_reference(
    con: duckdb.DuckDBPyConnection,
    reference: ValueExpr,
    group_by: tuple[Col, ...],
    tables: Mapping[str | None, Sequence[Mapping[str, Any]]],
) -> dict[tuple[Any, ...], float]:
    """The per-group reference total a relative tolerance scales by, run under the
    conservation's own grouping. Only an aggregate reduces to one value per group,
    so a non-aggregate reference is a contract error rather than a silently dropped
    scale. The proxy already requires a ``within(...)`` to precede ``relative_to``."""
    if not isinstance(reference, Agg):
        raise ContractError("relative_to(...) scales by an aggregate, one value per group")
    grouped = Agg(reference.func, reference.operand, group_by, reference.joined_on)
    return _run_side(con, grouped, tables, alias="ref")


def _run_side(
    con: duckdb.DuckDBPyConnection,
    agg: Agg,
    tables: Mapping[str | None, Sequence[Mapping[str, Any]]],
    *,
    alias: str,
) -> dict[tuple[Any, ...], float]:
    model = _single_model(agg)
    if model not in tables:
        raise ContractError(f"no rows supplied for model {model!r}")
    table_name = f"_dblect_{alias}"
    _load_table(con, table_name, tables[model])
    query = compile_aggregate_query(agg, table_name).sql(dialect="duckdb")
    result = con.execute(query).fetchall()
    out: dict[tuple[Any, ...], float] = {}
    key_width = len(agg.group_by)
    for row in result:
        key = tuple(row[:key_width])
        value = row[key_width]
        out[key] = float(value) if value is not None else 0.0
    return out


def _single_model(agg: Agg) -> str | None:
    """The one model an aggregate ranges over: every column it touches must share
    it, since a single-relation query is all we lower."""
    models = {c.model for c in _columns(agg)}
    if len(models) != 1:
        raise ContractError("a runnable aggregate must range over exactly one relation")
    return next(iter(models))


def _columns(expr: ValueExpr) -> list[Col]:
    if isinstance(expr, Col):
        return [expr]
    if isinstance(expr, Lit):
        return []
    if isinstance(expr, Arith):
        return _columns(expr.left) + _columns(expr.right)
    cols = _columns(expr.operand)
    for key in expr.group_by:
        cols.append(key)
    return cols


def _align(
    left: Mapping[tuple[Any, ...], float],
    right: Mapping[tuple[Any, ...], float],
    op: CmpOp,
    eps: float,
    ref: Mapping[tuple[Any, ...], float] | None = None,
) -> list[GroupMismatch]:
    # A relative tolerance reinterprets ``eps`` as a fraction of each group's
    # reference total, so the slack scales with the group rather than being one
    # flat absolute amount. A group absent from ``ref`` scales to zero (an exact
    # check), which is the conservative reading.
    mismatches: list[GroupMismatch] = []
    for key in sorted(set(left) | set(right), key=repr):
        lv = left.get(key)
        rv = right.get(key)
        group_eps = eps * abs(ref.get(key, 0.0)) if ref is not None else eps
        if lv is None or rv is None or not _holds(op, lv, rv, group_eps):
            mismatches.append(GroupMismatch(key, lv, rv))
    return mismatches


def _holds(op: CmpOp, left: float, right: float, eps: float) -> bool:
    if op is CmpOp.EQ:
        return abs(left - right) <= eps
    if op is CmpOp.NE:
        return abs(left - right) > eps
    if op is CmpOp.LT:
        return left < right
    if op is CmpOp.LE:
        return left <= right + eps
    if op is CmpOp.GT:
        return left > right
    return left + eps >= right


# --- duckdb loading -------------------------------------------------------------


def _load_table(
    con: duckdb.DuckDBPyConnection, name: str, rows: Sequence[Mapping[str, Any]]
) -> None:
    if not rows:
        raise ContractError(f"table {name!r} has no rows to infer a schema from")
    columns = list(rows[0].keys())
    # Quote every identifier through sqlglot: the column names come from the
    # supplied row dicts, so interpolating them raw would let an odd name reshape
    # the DDL. The table name is ours, but quoting it too keeps one rule.
    table = _quote(name)
    decls = ", ".join(f"{_quote(c)} {_column_type(rows, c)}" for c in columns)
    con.execute(f"CREATE TABLE {table} ({decls})")
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f"INSERT INTO {table} VALUES ({placeholders})",
        [[row.get(c) for c in columns] for row in rows],
    )


def _quote(identifier: str) -> str:
    return exp.to_identifier(identifier, quoted=True).sql(dialect="duckdb")


def _column_type(rows: Sequence[Mapping[str, Any]], column: str) -> str:
    for row in rows:
        value = row.get(column)
        if value is None:
            continue
        if isinstance(value, bool):
            return "BOOLEAN"
        if isinstance(value, (int, float)):
            return "DOUBLE"
        return "VARCHAR"
    return "VARCHAR"


def _has_aggregate(expr: ValueExpr) -> bool:
    if isinstance(expr, Agg):
        return True
    if isinstance(expr, Arith):
        return _has_aggregate(expr.left) or _has_aggregate(expr.right)
    return False


def _literal(value: float | str) -> Expr:
    return exp.Literal.string(value) if isinstance(value, str) else exp.Literal.number(value)


def _render_number(value: float) -> str:
    """A float that is integral renders without a trailing ``.0`` so the SQL reads
    the way an author wrote it (``0``, not ``0.0``)."""
    return str(int(value)) if value == int(value) else repr(value)
