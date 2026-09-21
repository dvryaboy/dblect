"""Empirical soundness PBT for the value-domain property: the oracle is
execution, not re-derivation.

The soundness invariant: **every ``Bounded`` set the property claims for a
column is a superset of that column's materialized, distinct, non-null
values.** An under-claim (a real value missing from the claimed set) is the
dangerous direction, since the check reads a stray-looking member as provably
dead. The anti-vacuity half (a property that always claimed ``Unbounded``
would trivially pass the superset check for the wrong reason) is closed by
asserting a passthrough is *exact*, not merely a superset.

The shared duckdb oracle (``tests/lineage/_duckdb_oracle.py``) materializes
integer columns; this property's semantics do not depend on a literal's kind
(string vs. numeric), so the generated alphabet is integers rather than the
strings a real ``status`` column would carry, sidestepping a kit change for a
type the oracle does not support today (see the report for this branch).

The grammar covers passthrough, rename, ``UNION ALL``, ``COALESCE`` with a
literal, ``CASE`` with and without ``ELSE``, a catch-all cast, and a
``WHERE ... IN`` filter, the same shapes the transfer-table tests pin by hand.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum, auto

import duckdb
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.predicate import Lit, LitKind
from dblect.lineage.properties.value_domain import (
    UNBOUNDED,
    Bounded,
    ValueDomain,
    value_domain_property,
)
from tests.lineage._duckdb_oracle import Table, materialized

_ALPHABET: tuple[int, ...] = (1, 2, 3)
_ALPHABET_SET = Bounded(frozenset(Lit(LitKind.NUM, Decimal(n)) for n in _ALPHABET))

_S1 = SourceRef(SourceKind.SOURCE, "source.test.raw.s1")
_S2 = SourceRef(SourceKind.SOURCE, "source.test.raw.s2")
_MODEL = SourceRef(SourceKind.MODEL, "model.test.m")
_SCHEMA = {"s1": {"status": "INTEGER"}, "s2": {"status": "INTEGER"}}


class Shape(StrEnum):
    PASSTHROUGH = auto()
    RENAME = auto()
    UNION = auto()
    COALESCE_LITERAL = auto()
    CASE_WITH_ELSE = auto()
    CASE_NO_ELSE = auto()
    CATCH_ALL_CAST = auto()
    WHERE_IN_FILTER = auto()


def _sql_of(shape: Shape) -> str:
    match shape:
        case Shape.PASSTHROUGH | Shape.RENAME:
            return "SELECT s.status AS r FROM s1 s"
        case Shape.UNION:
            return "SELECT a.status AS r FROM s1 a UNION ALL SELECT b.status AS r FROM s2 b"
        case Shape.COALESCE_LITERAL:
            return "SELECT COALESCE(s.status, 99) AS r FROM s1 s"
        case Shape.CASE_WITH_ELSE:
            return (
                "SELECT CASE WHEN s.status = 1 THEN 10 WHEN s.status = 2 THEN 20 "
                "ELSE 30 END AS r FROM s1 s"
            )
        case Shape.CASE_NO_ELSE:
            return (
                "SELECT CASE WHEN s.status = 1 THEN 10 WHEN s.status = 2 THEN 20 END AS r FROM s1 s"
            )
        case Shape.CATCH_ALL_CAST:
            return "SELECT CAST(s.status AS VARCHAR) AS r FROM s1 s"
        case Shape.WHERE_IN_FILTER:
            return "SELECT s.status AS r FROM s1 s WHERE s.status IN (1, 2)"


def _rows() -> st.SearchStrategy[tuple[int | None, ...]]:
    """A small run of ``status`` values, drawn from the declared alphabet plus
    NULL, so every materialized value is one the analysis was told about."""
    return st.lists(st.sampled_from((*_ALPHABET, None)), max_size=6).map(tuple)


@st.composite
def _scenario(draw: st.DrawFn) -> tuple[Shape, tuple[int | None, ...], tuple[int | None, ...]]:
    shape = draw(st.sampled_from(tuple(Shape)))
    rows1 = draw(_rows())
    rows2 = draw(_rows()) if shape is Shape.UNION else ()
    return shape, rows1, rows2


def _facts(scope: SourceRef) -> Mapping[ColumnRef, tuple[Fact[ValueDomain, ColumnRef], ...]]:
    ref = ColumnRef(scope, "status")
    return {
        ref: (
            Fact(scope=ref, value=_ALPHABET_SET, provenance=Declared(DeclaredSource.USER_ASSERTED)),
        )
    }


def _propagated(shape: Shape) -> ValueDomain:
    facts: dict[ColumnRef, tuple[Fact[ValueDomain, ColumnRef], ...]] = {}
    facts.update(_facts(_S1))
    if shape is Shape.UNION:
        facts.update(_facts(_S2))
    graph = build_model_graph(
        model_uid=_MODEL.unique_id,
        sql=_sql_of(shape),
        name_to_source={"s1": _S1, "s2": _S2},
        schema=_SCHEMA,
    )
    prop = value_domain_property(facts)
    anns = propagate(graph, prop)
    return anns[ColumnRef(_MODEL, "r")].value


@given(_scenario())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_claimed_bounded_set_covers_every_materialized_value(
    oracle_con: duckdb.DuckDBPyConnection,
    scenario: tuple[Shape, tuple[int | None, ...], tuple[int | None, ...]],
) -> None:
    shape, rows1, rows2 = scenario
    claimed = _propagated(shape)
    tables: list[Table] = [("s1", ("status",), [(v,) for v in rows1])]
    if shape is Shape.UNION:
        tables.append(("s2", ("status",), [(v,) for v in rows2]))
    with materialized(oracle_con, tables, _sql_of(shape)) as con:
        distinct = con.execute("SELECT DISTINCT r FROM _m WHERE r IS NOT NULL").fetchall()
        materialized_values = {row[0] for row in distinct}
        if isinstance(claimed, Bounded):
            claimed_values = {lit.value for lit in claimed.values}
            missing = materialized_values - claimed_values
            assert not missing, (
                f"shape={shape}: claimed {claimed_values} does not cover materialized "
                f"{materialized_values} (rows1={rows1!r} rows2={rows2!r})"
            )
        # An Unbounded claim makes no claim to falsify: trivially sound.


def test_passthrough_is_exact_not_just_a_superset() -> None:
    """The anti-vacuity check: a passthrough (or bare rename) must equal the
    declared alphabet exactly, not merely contain whatever the data happened
    to produce. A property that always widened to Unbounded would still pass
    the materialized superset check above; this one would not."""
    assert _propagated(Shape.PASSTHROUGH) == _ALPHABET_SET
    assert _propagated(Shape.RENAME) == _ALPHABET_SET


def test_catch_all_cast_is_unbounded_not_a_silent_narrow() -> None:
    """Sanity-checks that the catch-all shape actually exercises the
    Unbounded claim, so its arm above is not silently vacuous for a different
    reason than intended."""
    assert _propagated(Shape.CATCH_ALL_CAST) is UNBOUNDED
