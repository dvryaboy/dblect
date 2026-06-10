"""Empirical soundness PBT for the domain-type property: the oracle is unit rescaling.

The dimensional algebra rests on one theorem (Kennedy, *Dimension Types*, ESOP 1994):
a well-typed expression is invariant under the group action that rescales each unit
independently. Concretely, if the analyzer assigns an expression the dimension
``{usd: i, eur: j}``, then multiplying every ``usd``-tagged input by a scale ``a`` and
every ``eur``-tagged input by ``b`` must multiply the result by ``a^i * b^j``. That is
a property real data can witness: materialize the expression twice, once on the raw
inputs and once on the rescaled inputs, and the two outputs must differ by exactly the
factor the analyzer's dimension predicts.

So this test generates a small arithmetic expression over currency-tagged columns, asks
the analyzer for the output dimension, then materializes both datasets in duckdb and
checks the rescaling law. The data is the judge: an over-claimed dimension (calling a
cancelled ratio ``usd^1``, or a money-times-money product ``usd^1``) breaks the law and
the test catches it, with no rule restated. A mixed-currency expression the analyzer
reports as a conflict carries no dimension to predict a factor, so it is skipped here;
that the analyzer flags it is pinned in the propagation tests.

Only ``+``, ``*``, ``/`` over strictly positive inputs are generated, so every
subexpression is positive and division never hits a zero. Every leaf carries a known
currency, keeping the test on the dimensional algebra rather than the naked-composition
choice, which the propagation tests pin separately.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import duckdb
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    Concrete,
    Dimension,
    DomainTag,
    Tagged,
    domain_type_grounding,
    domain_type_property,
    tagged,
)

_SRC = SourceRef(SourceKind.SOURCE, "source.test.raw.t")
_MODEL = SourceRef(SourceKind.MODEL, "model.test.m")
_CURRENCIES = ("usd", "eur")
_SCALES: Mapping[str, float] = {"usd": 2.0, "eur": 3.0}


@dataclass(frozen=True)
class Expr:
    sql: str
    leaves: frozenset[str]  # the column names referenced


@dataclass(frozen=True)
class Scenario:
    columns: tuple[str, ...]
    currency_of: Mapping[str, str]  # column -> currency
    expr: Expr
    rows: tuple[tuple[float, ...], ...]  # one tuple per row, in `columns` order


@st.composite
def _expr(draw: st.DrawFn, columns: Sequence[str], depth: int) -> Expr:
    if depth == 0 or draw(st.booleans()):
        col = draw(st.sampled_from(columns))
        return Expr(sql=f"d.{col}", leaves=frozenset({col}))
    op = draw(st.sampled_from(("+", "*", "/")))
    left = draw(_expr(columns, depth - 1))
    right = draw(_expr(columns, depth - 1))
    return Expr(sql=f"({left.sql} {op} {right.sql})", leaves=left.leaves | right.leaves)


@st.composite
def _scenario(draw: st.DrawFn) -> Scenario:
    n = draw(st.integers(min_value=2, max_value=3))
    columns = tuple(f"c{i}" for i in range(n))
    currency_of = {c: draw(st.sampled_from(_CURRENCIES)) for c in columns}
    expr = draw(_expr(columns, depth=2))
    n_rows = draw(st.integers(min_value=1, max_value=5))
    rows = tuple(
        tuple(float(draw(st.integers(min_value=1, max_value=9))) for _ in columns)
        for _ in range(n_rows)
    )
    return Scenario(columns=columns, currency_of=currency_of, expr=expr, rows=rows)


def _model_sql(s: Scenario) -> str:
    return f"SELECT d.rid AS rid, {s.expr.sql} AS r FROM t AS d"


def _analyzer_dimension(s: Scenario) -> DomainTag:
    """The output column's domain tag, exactly as the column property derives it."""
    facts: dict[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]] = {}
    for col, currency in s.currency_of.items():
        ref = ColumnRef(_SRC, col)
        value = tagged(dimension=Dimension.of(Concrete(currency)))
        facts[ref] = (
            Fact(scope=ref, value=value, provenance=Declared(DeclaredSource.USER_ASSERTED)),
        )
    schema = {"t": {"rid": "INT", **dict.fromkeys(s.columns, "DOUBLE")}}
    graph = build_model_graph(
        model_uid=_MODEL.unique_id, sql=_model_sql(s), name_to_source={"t": _SRC}, schema=schema
    )
    anns = propagate(graph, domain_type_property(domain_type_grounding(facts)))
    return anns[ColumnRef(_MODEL, "r")].value


def _predicted_factor(dimension: Dimension) -> float:
    factor = 1.0
    for unit, power in dimension.exponents:
        assert isinstance(unit, Concrete)
        factor *= _SCALES[unit.name] ** power
    return factor


def _materialize(s: Scenario, *, scaled: bool) -> list[float]:
    con = duckdb.connect(":memory:")
    try:
        cols_ddl = ", ".join(f"{c} DOUBLE" for c in s.columns)
        con.execute(f"CREATE TABLE t (rid INTEGER, {cols_ddl})")
        placeholders = ", ".join(["?"] * (len(s.columns) + 1))
        payload = [
            [
                rid,
                *(
                    _scale_value(v, s.currency_of[col], scaled)
                    for col, v in zip(s.columns, row, strict=True)
                ),
            ]
            for rid, row in enumerate(s.rows)
        ]
        con.executemany(f"INSERT INTO t VALUES ({placeholders})", payload)
        result = con.execute(f"SELECT r FROM ({_model_sql(s)}) sub ORDER BY rid").fetchall()
        return [float(r[0]) for r in result]
    finally:
        con.close()


def _scale_value(value: float, currency: str, scaled: bool) -> float:
    return value * _SCALES[currency] if scaled else value


@given(_scenario())
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_dimension_claim_predicts_unit_rescaling(s: Scenario) -> None:
    """The analyzer's output dimension predicts exactly how the materialized result
    transforms under independent unit rescaling. A conflict or naked output carries no
    dimension to predict, so it is skipped; a known dimension must match the data."""
    value = _analyzer_dimension(s)
    if not isinstance(value, Tagged) or value.dimension is None:
        return  # conflict (mixed currency) or naked: no dimension to witness
    factor = _predicted_factor(value.dimension)
    raw = _materialize(s, scaled=False)
    rescaled = _materialize(s, scaled=True)
    assert len(raw) == len(rescaled)
    for original, scaled_result in zip(raw, rescaled, strict=True):
        expected = original * factor
        tolerance = 1e-6 * max(1.0, abs(expected))
        assert abs(scaled_result - expected) <= tolerance, (
            f"rescaling law broken: dimension {value.dimension} predicts factor {factor}, "
            f"but {original} -> {scaled_result} (expected {expected}) for sql={s.expr.sql!r} "
            f"currencies={dict(s.currency_of)}"
        )
