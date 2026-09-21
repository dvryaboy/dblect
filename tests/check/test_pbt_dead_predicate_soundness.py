"""Empirical soundness PBT for the dead-predicate decision procedure: the
oracle is execution, not re-derivation.

This is the iff that makes the check's decision procedure exact rather than
approximate, over materialized data drawn straight from the declared alphabet
plus NULL:

* a ``DEAD_PREDICATE`` verdict on a top-level ``WHERE`` atom: the materialized
  result is empty;
* a ``REDUNDANT_PREDICATE`` verdict: the result equals the same query with the
  atom replaced by ``status IS NOT NULL``;
* a dead atom projected as a bare scalar: no row carries ``TRUE``;
* a literal the decision procedure does *not* flag (an exact member), when a
  generator includes that value among the rows: the ``WHERE`` result is
  non-empty (the completing direction: silence is not overclaiming either).

``test_pbt_value_domain_soundness.py`` pins the lineage layer's propagation
this way, over an integer alphabet for the same oracle-compatibility reason
given there; this test pins the check layer's own literal-vs-domain decision
the same way, against the same kind of oracle.
"""

from __future__ import annotations

from decimal import Decimal

import duckdb
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.check.dead_predicate import BoolContext, CompForm, atom_verdict
from dblect.check.findings import CheckFindingKind
from dblect.lineage.predicate import Lit, LitKind
from dblect.lineage.properties.value_domain import Bounded
from tests.lineage._duckdb_oracle import Table, materialized, scalar

_ALPHABET: tuple[int, ...] = (1, 2, 3)
_DOMAIN = Bounded(frozenset(Lit(LitKind.NUM, Decimal(n)) for n in _ALPHABET))

# Members, same-kind strays, and (deliberately excluded) case-variants: this
# PBT covers DEAD_PREDICATE/REDUNDANT_PREDICATE/projected/member, not the
# collation-dependent case-only kind (numbers have no case to begin with).
_LITERALS: tuple[int, ...] = (*_ALPHABET, 4, 5, 9)


def _rows() -> st.SearchStrategy[tuple[int | None, ...]]:
    return st.lists(st.sampled_from((*_ALPHABET, None)), max_size=6).map(tuple)


@st.composite
def _scenario(draw: st.DrawFn) -> tuple[tuple[int | None, ...], int, CompForm]:
    rows = draw(_rows())
    literal = draw(st.sampled_from(_LITERALS))
    form = draw(st.sampled_from((CompForm.EQ, CompForm.NEQ)))
    return rows, literal, form


def _sql(form: CompForm, literal: int) -> str:
    op = "=" if form is CompForm.EQ else "!="
    return f"SELECT status FROM t WHERE status {op} {literal}"


@given(_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_decision_procedure_matches_materialized_data(
    oracle_con: duckdb.DuckDBPyConnection,
    scenario: tuple[tuple[int | None, ...], int, CompForm],
) -> None:
    rows, literal_value, form = scenario
    lit = Lit(LitKind.NUM, Decimal(literal_value))
    verdict = atom_verdict(
        form, _DOMAIN, lit, BoolContext.WHERE_LIKE, column="status", negated=False
    )
    tables: list[Table] = [("t", ("status",), [(v,) for v in rows])]

    if verdict is not None and verdict.kind is CheckFindingKind.DEAD_PREDICATE:
        with materialized(oracle_con, tables, _sql(form, literal_value)) as con:
            n = scalar(con, "SELECT COUNT(*) FROM _m")
            assert n == 0, f"DEAD_PREDICATE claimed but materialized {n} rows: {scenario!r}"
        return

    if verdict is not None and verdict.kind is CheckFindingKind.REDUNDANT_PREDICATE:
        with materialized(oracle_con, tables, _sql(form, literal_value)) as con:
            claimed = scalar(con, "SELECT COUNT(*) FROM _m")
        not_null_sql = "SELECT status FROM t WHERE status IS NOT NULL"
        with materialized(oracle_con, tables, not_null_sql) as con:
            not_null = scalar(con, "SELECT COUNT(*) FROM _m")
        assert claimed == not_null, (
            f"REDUNDANT_PREDICATE claimed equal to IS NOT NULL but got {claimed} vs "
            f"{not_null}: {scenario!r}"
        )
        return

    # Silent: either a member (in which case a generator that included it must
    # produce a non-empty WHERE result) or a kind/case verdict this PBT does
    # not exercise. Only EQ's member case is asserted; NEQ's member case is
    # not "the completing direction" this item names.
    if verdict is None and form is CompForm.EQ and literal_value in rows:
        with materialized(oracle_con, tables, _sql(form, literal_value)) as con:
            n = scalar(con, "SELECT COUNT(*) FROM _m")
        assert n > 0, f"member literal present in rows produced an empty result: {scenario!r}"


def _projected_sql(literal: int) -> str:
    return f"SELECT status = {literal} AS r FROM t"


@given(_rows(), st.sampled_from(_LITERALS))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_projected_dead_atom_never_carries_true(
    oracle_con: duckdb.DuckDBPyConnection, rows: tuple[int | None, ...], literal_value: int
) -> None:
    lit = Lit(LitKind.NUM, Decimal(literal_value))
    verdict = atom_verdict(
        CompForm.EQ, _DOMAIN, lit, BoolContext.PROJECTED, column="status", negated=False
    )
    if verdict is None or verdict.kind is not CheckFindingKind.DEAD_PREDICATE:
        return
    tables: list[Table] = [("t", ("status",), [(v,) for v in rows])]
    with materialized(oracle_con, tables, _projected_sql(literal_value)) as con:
        n = scalar(con, "SELECT COUNT(*) FROM _m WHERE r")
        assert n == 0, f"projected dead atom carried TRUE on {n} rows: rows={rows!r}"
