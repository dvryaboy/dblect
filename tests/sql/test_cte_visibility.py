"""``cte_of`` binds a bare table reference to the CTE it can lexically see."""

from __future__ import annotations

import pytest
from sqlglot import exp

from dblect.sql import _sqlglot as sg
from dblect.sql import parse_sql


def _binding(sql: str, reference: str) -> str | None:
    """The alias of the CTE the reference aliased ``reference`` reads, if any."""
    (table,) = [
        t for t in parse_sql(sql, dialect="duckdb").find_all(exp.Table) if t.alias == reference
    ]
    cte = sg.cte_of(table)
    return None if cte is None else cte.alias


@pytest.mark.parametrize(
    ("sql", "bound"),
    [
        # a body sees the siblings declared before it, not itself or the ones after
        ("with t as (select 1 a), u as (select a from t r) select * from u", "t"),
        ("with u as (select a from t r), t as (select 1 a) select * from u", None),
        ("with t as (select a from t r) select * from t", None),
        ("with t as (select a from t) select * from t r", "t"),
        # RECURSIVE lets every body see every CTE, itself included
        ("with recursive t as (select a from t r) select * from t", "t"),
        ("with recursive u as (select a from t r), t as (select 1 a) select * from u", "t"),
        # names compare without case
        ("with T as (select 1 a) select * from t r", "T"),
        ("with t as (select 1 a) select * from T r", "t"),
        # a schema-qualified reference is the relation; a name no WITH declares binds nothing
        ("with t as (select 1 a) select * from s.t r", None),
        ("select * from t r", None),
        # the nearest WITH wins; a nested one is invisible outside its select
        ("with t as (select 1 a) select * from (with t as (select 2 a) select * from t r) q", "t"),
        ("with u as (select 1 a) select * from (with t as (select 2 a) select 1) q, t r", None),
    ],
)
def test_a_reference_binds_the_cte_it_can_see(sql: str, bound: str | None) -> None:
    assert _binding(sql, "r") == bound
