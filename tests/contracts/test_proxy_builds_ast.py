"""The proxy surface folds the documented AST.

These pin the contract between what an author writes (``self.col.sum()``,
``models.m.c``, arithmetic, comparison, fact constructors) and the AST nodes the
framework reads. They are boundary tests: they survive any change to how the
proxies are implemented as long as the same expression still produces the same
tree.
"""

from __future__ import annotations

import pytest

from dblect.contracts import ContractError, ast, models
from dblect.contracts.proxy import ColumnProxy, ContractSelf


def _self() -> ContractSelf:
    return ContractSelf()


def test_self_column_is_own_model() -> None:
    col = _self().amount
    assert isinstance(col, ColumnProxy)
    assert col.col == ast.Col(None, "amount")


def test_models_column_names_its_model() -> None:
    assert models.stg_orders.subtotal.col == ast.Col("stg_orders", "subtotal")


def test_indexing_reaches_columns_that_collide_with_proxy_names() -> None:
    """A column whose name shadows a proxy method or slot (``key``/``grain`` on
    self, ``model`` on a model reference) is reachable through indexing, which
    never resolves to the shadowing member."""
    s = _self()
    assert s["key"].col == ast.Col(None, "key")
    assert s["grain"].col == ast.Col(None, "grain")
    assert models.dim_customers["model"].col == ast.Col("dim_customers", "model")


def test_sum_group_by_builds_grouped_aggregate() -> None:
    s = _self()
    agg = s.order_total.sum().group_by(s.order_id)
    assert agg.expr == ast.Agg(
        ast.AggFunc.SUM, ast.Col(None, "order_total"), (ast.Col(None, "order_id"),)
    )


def test_arithmetic_nests_left_to_right() -> None:
    s = _self()
    expr = (s.a + s.b) / 2
    assert expr.expr == ast.Arith(
        ast.ArithOp.DIV,
        ast.Arith(ast.ArithOp.ADD, ast.Col(None, "a"), ast.Col(None, "b")),
        ast.Lit(2.0),
    )


def test_reflected_arithmetic_keeps_operand_order() -> None:
    expr = 100 - _self().discount
    assert expr.expr == ast.Arith(ast.ArithOp.SUB, ast.Lit(100.0), ast.Col(None, "discount"))


def test_equality_of_aggregates_with_tolerance() -> None:
    s = _self()
    pred = (s.order_total.sum() == models.lines.subtotal.sum()).within(0.01)
    assert pred.pred == ast.Compare(
        ast.CmpOp.EQ,
        ast.Agg(ast.AggFunc.SUM, ast.Col(None, "order_total")),
        ast.Agg(ast.AggFunc.SUM, ast.Col("lines", "subtotal")),
        ast.Tolerance(0.01),
    )


def test_relative_tolerance_scales_an_absolute_one() -> None:
    s = _self()
    pred = (s.a.sum() == s.b.sum()).within(0.05).relative_to(s.b.sum())
    assert isinstance(pred.pred, ast.Compare)
    assert pred.pred.tolerance == ast.Tolerance(0.05, ast.Agg(ast.AggFunc.SUM, ast.Col(None, "b")))


def test_within_on_a_non_equality_is_a_contract_error() -> None:
    s = _self()
    with pytest.raises(ContractError):
        (s.a.sum() < s.b.sum()).within(0.01)


def test_relative_to_without_within_is_a_contract_error() -> None:
    s = _self()
    with pytest.raises(ContractError):
        (s.a.sum() == s.b.sum()).relative_to(s.b.sum())


def test_boolean_combinators() -> None:
    s = _self()
    pred = (s.a > 0) & s.b.is_not_null()
    assert pred.pred == ast.BoolNode(
        ast.BoolOp.AND,
        (
            ast.Compare(ast.CmpOp.GT, ast.Col(None, "a"), ast.Lit(0.0)),
            ast.IsNull(ast.Col(None, "b"), negated=True),
        ),
    )


# --- fact constructors ----------------------------------------------------------


def test_determines_builds_the_fd_fact() -> None:
    s = _self()
    fact = s.country.determines(s.currency)
    assert fact.fact == ast.DeterminesFact((ast.Col(None, "country"),), ast.Col(None, "currency"))


def test_references_builds_the_edge() -> None:
    fact = _self().customer_id.references(models.dim_customers.customer_id)
    assert fact.fact == ast.ReferencesFact(
        ast.Col(None, "customer_id"), ast.Col("dim_customers", "customer_id")
    )


def test_key_and_grain() -> None:
    s = _self()
    assert s.key(s.a, s.b).fact == ast.KeyFact((ast.Col(None, "a"), ast.Col(None, "b")))
    assert s.grain(per=s.order_id).fact == ast.GrainFact((ast.Col(None, "order_id"),))


def test_grain_accepts_a_tuple() -> None:
    s = _self()
    assert s.grain(per=(s.a, s.b)).fact == ast.GrainFact((ast.Col(None, "a"), ast.Col(None, "b")))


def test_group_by_rejects_a_non_column() -> None:
    s = _self()
    with pytest.raises(ContractError):
        s.amount.sum().group_by(s.a + s.b)  # type: ignore[arg-type]
