"""Lowering the AST to SQL, and running a conservation predicate.

The round-trip cases pin the SQL a value or predicate renders to; the execution
cases pin what the conservation evaluator concludes over known data. Both are the
contract a downstream consumer (the execution loop, a reporter) relies on.
"""

from __future__ import annotations

import pytest

from dblect.contracts import ContractError, ast
from dblect.contracts.compile import (
    compile_aggregate_query,
    compile_predicate,
    compile_value,
    evaluate_predicate,
)


def _sql(node: object) -> str:
    assert hasattr(node, "sql")
    return node.sql(dialect="duckdb")  # type: ignore[attr-defined]


# --- value rendering ------------------------------------------------------------


def test_column_renders_bare_and_qualified() -> None:
    assert _sql(compile_value(ast.Col(None, "amount"))) == "amount"
    assert _sql(compile_value(ast.Col("stg", "amount"))) == "stg.amount"


def test_aggregate_calls() -> None:
    assert _sql(compile_value(ast.Agg(ast.AggFunc.SUM, ast.Col(None, "x")))) == "SUM(x)"
    assert (
        _sql(compile_value(ast.Agg(ast.AggFunc.COUNT_DISTINCT, ast.Col(None, "x"))))
        == "COUNT(DISTINCT x)"
    )


def test_arithmetic_renders() -> None:
    expr = ast.Arith(
        ast.ArithOp.DIV,
        ast.Arith(ast.ArithOp.ADD, ast.Col(None, "a"), ast.Col(None, "b")),
        ast.Lit(2),
    )
    assert _sql(compile_value(expr)) == "(a + b) / 2"


def test_grouped_query() -> None:
    agg = ast.Agg(ast.AggFunc.SUM, ast.Col(None, "amount"), (ast.Col(None, "country"),))
    assert (
        _sql(compile_aggregate_query(agg, "payments"))
        == "SELECT country, SUM(amount) AS value FROM payments GROUP BY country"
    )


def test_ungrouped_query() -> None:
    agg = ast.Agg(ast.AggFunc.SUM, ast.Col(None, "amount"))
    assert (
        _sql(compile_aggregate_query(agg, "payments"))
        == "SELECT SUM(amount) AS value FROM payments"
    )


# --- predicate rendering --------------------------------------------------------


def test_row_predicates_render() -> None:
    assert _sql(compile_predicate(ast.IsNull(ast.Col(None, "z")))) == "z IS NULL"
    assert _sql(compile_predicate(ast.IsNull(ast.Col(None, "z"), negated=True))) == "NOT z IS NULL"
    assert _sql(compile_predicate(ast.Between(ast.Col(None, "z"), 0, 10))) == "z BETWEEN 0 AND 10"


def test_boolean_predicate_renders() -> None:
    pred = ast.BoolNode(
        ast.BoolOp.AND,
        (
            ast.Compare(ast.CmpOp.GT, ast.Col(None, "a"), ast.Lit(0)),
            ast.IsNull(ast.Col(None, "b"), negated=True),
        ),
    )
    assert _sql(compile_predicate(pred)) == "a > 0 AND NOT b IS NULL"


def test_aggregate_comparison_is_not_a_row_predicate() -> None:
    pred = ast.Compare(
        ast.CmpOp.EQ,
        ast.Agg(ast.AggFunc.SUM, ast.Col(None, "a")),
        ast.Agg(ast.AggFunc.SUM, ast.Col(None, "b")),
    )
    with pytest.raises(ContractError):
        compile_predicate(pred)


# --- running a conservation check -----------------------------------------------


def _conservation(left_model: str | None, right_model: str | None) -> ast.Compare:
    return ast.Compare(
        ast.CmpOp.EQ,
        ast.Agg(ast.AggFunc.SUM, ast.Col(left_model, "amount"), (ast.Col(left_model, "k"),)),
        ast.Agg(ast.AggFunc.SUM, ast.Col(right_model, "amount"), (ast.Col(right_model, "k"),)),
        ast.Tolerance(0.0),
    )


def test_conservation_holds_when_per_group_sums_agree() -> None:
    rows = [
        {"k": "a", "amount": 10},
        {"k": "a", "amount": 5},
        {"k": "b", "amount": 7},
    ]
    pred = _conservation(None, "other")
    result = evaluate_predicate(pred, {None: rows, "other": list(rows)})
    assert result.ok
    assert result.mismatches == ()


def test_conservation_reports_the_off_group() -> None:
    left = [{"k": "a", "amount": 10}, {"k": "b", "amount": 7}]
    right = [{"k": "a", "amount": 10}, {"k": "b", "amount": 8}]
    result = evaluate_predicate(_conservation(None, "other"), {None: left, "other": right})
    assert not result.ok
    assert [m.key for m in result.mismatches] == [("b",)]


def test_tolerance_absorbs_small_drift() -> None:
    left = [{"k": "a", "amount": 100.0}]
    right = [{"k": "a", "amount": 100.005}]
    pred = ast.Compare(
        ast.CmpOp.EQ,
        ast.Agg(ast.AggFunc.SUM, ast.Col(None, "amount"), (ast.Col(None, "k"),)),
        ast.Agg(ast.AggFunc.SUM, ast.Col("other", "amount"), (ast.Col("other", "k"),)),
        ast.Tolerance(0.01),
    )
    assert evaluate_predicate(pred, {None: left, "other": right}).ok


def test_relative_tolerance_scales_each_group_by_a_reference() -> None:
    """``within(0.05).relative_to(own.sum())`` checks 5% of each group's reference
    total, not one flat absolute slack: the small group is held to a proportionally
    tighter bound than the large one. An absolute 0.05 would flag both groups."""
    own = [{"k": "a", "amount": 1000.0}, {"k": "b", "amount": 10.0}]
    other = [{"k": "a", "amount": 1020.0}, {"k": "b", "amount": 12.0}]
    reference = ast.Agg(ast.AggFunc.SUM, ast.Col(None, "amount"))
    pred = ast.Compare(
        ast.CmpOp.EQ,
        ast.Agg(ast.AggFunc.SUM, ast.Col(None, "amount"), (ast.Col(None, "k"),)),
        ast.Agg(ast.AggFunc.SUM, ast.Col("other", "amount"), (ast.Col("other", "k"),)),
        ast.Tolerance(0.05, reference),
    )
    result = evaluate_predicate(pred, {None: own, "other": other})
    # group a: |1000 - 1020| = 20 <= 5% of 1000 (= 50), holds.
    # group b: |10 - 12|     =  2  > 5% of 10   (= 0.5), off.
    assert [m.key for m in result.mismatches] == [("b",)]


def test_relative_tolerance_reference_must_reduce_to_one_value_per_group() -> None:
    """A bare column cannot scale a grouped check; only an aggregate reference can."""
    rows = [{"k": "a", "amount": 1.0}]
    pred = ast.Compare(
        ast.CmpOp.EQ,
        ast.Agg(ast.AggFunc.SUM, ast.Col(None, "amount"), (ast.Col(None, "k"),)),
        ast.Agg(ast.AggFunc.SUM, ast.Col("other", "amount"), (ast.Col("other", "k"),)),
        ast.Tolerance(0.05, ast.Col(None, "amount")),
    )
    with pytest.raises(ContractError):
        evaluate_predicate(pred, {None: rows, "other": list(rows)})


def test_inequality_conservation() -> None:
    """``returns <= orders`` per group: a refund never exceeds the order."""
    orders = [{"k": "a", "amount": 100}, {"k": "b", "amount": 50}]
    returns = [{"k": "a", "amount": 30}, {"k": "b", "amount": 60}]
    pred = ast.Compare(
        ast.CmpOp.LE,
        ast.Agg(ast.AggFunc.SUM, ast.Col(None, "amount"), (ast.Col(None, "k"),)),
        ast.Agg(ast.AggFunc.SUM, ast.Col("orders", "amount"), (ast.Col("orders", "k"),)),
    )
    result = evaluate_predicate(pred, {None: returns, "orders": orders})
    assert [m.key for m in result.mismatches] == [("b",)]
