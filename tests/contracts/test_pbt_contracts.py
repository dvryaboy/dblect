"""Property-based contracts for the AST→SQL path and the conservation evaluator.

Two laws hold this layer together. Every value expression we emit must be valid,
stable SQL: rendering it, parsing it back, and rendering again is a fixed point
(if it were not, a downstream tool re-reading our SQL would see something we did
not mean). And the conservation evaluator must agree with arithmetic done in
plain Python: the per-group sums it compares are exactly the sums of the rows,
so an equality holds precisely when those sums agree and a fan-out that doubles
one side breaks exactly the groups whose total is nonzero.
"""

from __future__ import annotations

import sqlglot
from hypothesis import given
from hypothesis import strategies as st

from dblect.contracts import ast
from dblect.contracts.compile import compile_value, evaluate_predicate

_NAMES = st.sampled_from(["a", "b", "amount", "order_id", "subtotal"])
_MODELS = st.sampled_from([None, "stg", "lines"])

_COLS = st.builds(ast.Col, _MODELS, _NAMES)
_LITS = st.builds(
    ast.Lit, st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False)
)


def _extend(children: st.SearchStrategy[ast.ValueExpr]) -> st.SearchStrategy[ast.ValueExpr]:
    arith = st.builds(ast.Arith, st.sampled_from(list(ast.ArithOp)), children, children)
    aggs = st.builds(ast.Agg, st.sampled_from(list(ast.AggFunc)), children)
    return st.one_of(arith, aggs)


_VALUES: st.SearchStrategy[ast.ValueExpr] = st.recursive(
    st.one_of(_COLS, _LITS), _extend, max_leaves=8
)


@given(expr=_VALUES)
def test_emitted_sql_is_a_stable_fixed_point(expr: ast.ValueExpr) -> None:
    """compile → render → parse → render is idempotent: the SQL we emit is valid
    and round-trips through sqlglot unchanged."""
    rendered = compile_value(expr).sql(dialect="duckdb")
    reparsed = sqlglot.parse_one(rendered, dialect="duckdb").sql(dialect="duckdb")
    assert reparsed == rendered


# --- the conservation evaluator agrees with Python arithmetic --------------------

_ROWS = st.lists(
    st.fixed_dictionaries(
        {
            "k": st.sampled_from(["x", "y", "z"]),
            "amount": st.integers(min_value=-1000, max_value=1000),
        }
    ),
    min_size=1,
    max_size=30,
)


def _grouped_sum(rows: list[dict[str, object]]) -> dict[tuple[object, ...], int]:
    out: dict[tuple[object, ...], int] = {}
    for row in rows:
        key = (row["k"],)
        out[key] = out.get(key, 0) + int(row["amount"])  # type: ignore[call-overload]
    return out


def _conservation(left_model: str | None, right_model: str | None) -> ast.Compare:
    return ast.Compare(
        ast.CmpOp.EQ,
        ast.Agg(ast.AggFunc.SUM, ast.Col(left_model, "amount"), (ast.Col(left_model, "k"),)),
        ast.Agg(ast.AggFunc.SUM, ast.Col(right_model, "amount"), (ast.Col(right_model, "k"),)),
    )


@given(rows=_ROWS)
def test_reflexive_conservation_always_holds(rows: list[dict[str, object]]) -> None:
    result = evaluate_predicate(_conservation(None, "other"), {None: rows, "other": list(rows)})
    assert result.ok


@given(rows=_ROWS)
def test_doubling_one_side_breaks_exactly_the_nonzero_groups(
    rows: list[dict[str, object]],
) -> None:
    """A fan-out that replicates every row doubles each group's sum, so equality
    fails on exactly the groups whose total is nonzero (a zero stays zero)."""
    doubled = rows + rows
    result = evaluate_predicate(_conservation(None, "lines"), {None: rows, "lines": doubled})
    broken = {m.key for m in result.mismatches}
    expected = {key for key, total in _grouped_sum(rows).items() if total != 0}
    assert broken == expected
