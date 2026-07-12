"""Property-based tests for two structural detectors, complementing the example tests
in ``test_patterns.py``.

Three properties with teeth for the precision fixes these detectors carry:

* **Dialect invariance of the inner-flatten detector** (parse-only, metamorphic): the same
  logical array flatten written under ``UNNEST`` (duckdb/bigquery), ``LATERAL VIEW EXPLODE``
  (spark), and ``LATERAL FLATTEN`` (snowflake) must yield the same number of findings. The
  detector reads a hazard off the structural shape, so the surface dialect must not change the
  verdict. This is the invariant the explode/flatten non-emptiness fix restored: before it, a
  provably non-empty literal array cleared under ``UNNEST`` but fired under ``EXPLODE``.

* **Soundness of the inner-flatten clear** (duckdb execution oracle): if the detector clears
  an inner flatten, materializing ``t, UNNEST(arr)`` drops no parent row. The flatten clear is
  a pure semantic claim ("provably non-empty"), so this holds over a broad array grammar that
  reaches empty arrays (an empty cast, an inverted generator, an empty subquery-array); the
  oracle catches an unsound clear rather than confirming a known-good subset.

* **Soundness of the where-inversion clear** (duckdb execution oracle): for
  ``a LEFT JOIN b ... WHERE <predicate>`` where the predicate is a null-intolerant comparison
  over the optional side under one of a closed set of wrappers, if the detector clears the
  predicate then materialized data keeps every unmatched row. The oracle is the data, so a
  clear that actually drops unmatched rows fails here against ground truth rather than against a
  re-derivation. The wrapper axis (``_WHERE_WRAPS``) is enumerated, not sampled, and includes
  the traps a defaulted ``TRUE`` still drops on: ``coalesce(cmp, false)``, ``not coalesce(cmp,
  true)``, and ``coalesce(cmp, true) = false``. The data around each wrapper is sampled.

  The wrappers stay inside the comparison-plus-``COALESCE`` grammar the ``defaulted_true_by_
  coalesce`` guard reasons about, where a clear is meant to imply preservation. They do not
  include the column-level ``COALESCE(b.col, 0) = x`` idiom, whose permissive clear is a
  deliberate "the analyst handled the null" call rather than a preservation claim (see
  ``guards.is_coalesced``); that idiom's contract is not "the join is preserved".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import duckdb
import pytest
import sqlglot
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.sql.patterns import (
    detect_inner_flatten_row_drop,
    detect_where_on_outer_joined_nullable,
)
from tests.lineage._duckdb_oracle import materialized, scalar

# --- dialect invariance of the inner-flatten detector ------------------------------------


def _flatten_stmt(dialect: str, array_sql: str, *, outer: bool) -> str:
    """The inner (or row-preserving outer) flatten of ``array_sql`` in ``dialect``'s syntax."""
    if dialect in ("duckdb", "bigquery"):
        arm = f"unnest({array_sql}) as x"
        return (
            f"select t.id, x from t left join {arm} on true"
            if outer
            else (f"select t.id, x from t, {arm}")
        )
    if dialect == "spark":
        kw = "outer explode" if outer else "explode"
        return f"select t.id, x from t lateral view {kw}({array_sql}) tt as x"
    if dialect == "snowflake":
        tail = ", outer => true" if outer else ""
        return f"select t.id, f.value from t, lateral flatten(input => {array_sql}{tail}) f"
    raise AssertionError(dialect)


@st.composite
def _array_spellings(draw: st.DrawFn) -> dict[str, str]:
    """One logical array, rendered per dialect. Every entry denotes the *same* array, so the
    detector must return the same verdict for all of them."""
    kind = draw(st.sampled_from(["nonempty_literal", "empty_literal", "column", "series"]))
    if kind == "nonempty_literal":
        elems = ", ".join(
            str(draw(st.integers(min_value=0, max_value=9)))
            for _ in range(draw(st.integers(min_value=1, max_value=4)))
        )
        return {
            "bigquery": f"[{elems}]",
            "spark": f"array({elems})",
            "snowflake": f"array_construct({elems})",
        }
    if kind == "empty_literal":
        return {"bigquery": "[]", "spark": "array()", "snowflake": "array_construct()"}
    if kind == "column":
        return {"bigquery": "t.arr", "spark": "t.arr", "snowflake": "t.arr"}
    # A numeric spine: duckdb/bigquery `generate_series`/`generate_array` and spark `sequence`
    # all parse to a generator; snowflake has no flatten-over-generator idiom, so it sits out.
    lo, hi = (
        draw(st.integers(min_value=0, max_value=5)),
        draw(st.integers(min_value=0, max_value=9)),
    )
    return {"bigquery": f"generate_array({lo}, {hi})", "spark": f"sequence({lo}, {hi})"}


@given(spellings=_array_spellings(), outer=st.booleans())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_inner_flatten_verdict_is_dialect_invariant(spellings: dict[str, str], outer: bool) -> None:
    counts = {
        dialect: len(detect_inner_flatten_row_drop(sqlglot.parse_one(stmt, read=dialect)))
        for dialect, array_sql in spellings.items()
        if (stmt := _flatten_stmt(dialect, array_sql, outer=outer))
    }
    assert len(set(counts.values())) == 1, (
        f"dialect-dependent inner-flatten verdict {counts} (outer={outer}) for {spellings}"
    )


# --- soundness of the inner-flatten clear (duckdb execution oracle) ----------------------
#
# Unlike the where detector, whose clear is an intent judgement (an explicit COALESCE clears
# even when it drops rows), the inner-flatten clear is a pure semantic claim: "this array is
# provably non-empty, so no parent row drops". That claim IS oracle-testable over a broad
# array grammar: if the detector clears, materializing the UNNEST must drop no parent row. A
# generator that reaches empty arrays (an empty cast, an inverted generator, an empty
# subquery-array) hunts for an unsound clear rather than confirming a known-good subset.


@pytest.fixture(scope="session")
def flatten_con(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    con.execute("create or replace table t(id integer)")
    con.execute("insert into t values (1), (2), (3)")
    con.execute("create or replace table vals(x integer)")
    con.execute("insert into vals values (5), (6)")
    return con


@st.composite
def _executable_array(draw: st.DrawFn) -> str:
    """A duckdb-executable array expression, reaching both non-empty and empty arrays so the
    oracle can catch an unsound clear."""
    kind = draw(st.sampled_from(["literal", "empty_cast", "series", "subquery"]))
    if kind == "literal":
        return (
            "["
            + ", ".join(str(draw(st.integers(0, 9))) for _ in range(draw(st.integers(1, 4))))
            + "]"
        )
    if kind == "empty_cast":
        return "CAST([] AS INTEGER[])"
    if kind == "series":
        return f"generate_series({draw(st.integers(0, 6))}, {draw(st.integers(0, 6))})"
    return f"(select array_agg(x) from vals where x > {draw(st.integers(0, 10))})"


@given(array_sql=_executable_array())
@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_inner_flatten_clear_implies_no_parent_row_dropped(
    flatten_con: duckdb.DuckDBPyConnection, array_sql: str
) -> None:
    """If the detector clears an inner flatten, running ``t, UNNEST(arr)`` drops no parent row.
    The array is a constant, so every parent row sees the same array: a clear that is wrong
    about non-emptiness drops all parent rows here and fails."""
    model_sql = f"select t.id from t, unnest({array_sql}) as u"
    cleared = len(detect_inner_flatten_row_drop(sqlglot.parse_one(model_sql, read="duckdb"))) == 0
    total = scalar(flatten_con, "select count(*) from t")
    survivors = scalar(flatten_con, f"select count(distinct id) from ({model_sql}) s")
    if cleared:
        assert total - survivors == 0, (
            f"detector cleared but {total - survivors} parent rows dropped for unnest({array_sql})"
        )


# --- soundness of the where-inversion clear ----------------------------------------------


# The closed set of predicate shapes wrapping a null-intolerant comparison ``cmp`` on the
# optional side. Each fixes how an unmatched-row NULL is treated, and thus whether the LEFT
# JOIN's unmatched rows survive. The wrap axis is enumerated, not sampled: it is closed and the
# guard must decide every member. `bare` and `coalesce(cmp, false)` drop the padded rows;
# `coalesce(cmp, true)` (bare, or under a top-level AND) keeps them; `coalesce(cmp, a.v > 0)`
# defers to the preserved side. The last two are the soundness traps -- a defaulted TRUE that a
# NOT or a re-comparison flips back into a drop -- which a clear must not silence.
_WHERE_WRAPS: tuple[str, ...] = (
    "{cmp}",
    "coalesce({cmp}, true)",
    "coalesce({cmp}, false)",
    "coalesce({cmp}, a.v > 0)",
    "coalesce({cmp}, true) and a.k >= 0",
    "not coalesce({cmp}, true)",
    "coalesce({cmp}, true) = false",
)


@dataclass(frozen=True, slots=True)
class WhereData:
    a_rows: tuple[tuple[int, int], ...]
    b_rows: tuple[tuple[int, int], ...]
    threshold: int


@st.composite
def _where_data(draw: st.DrawFn) -> WhereData:
    n = draw(st.integers(min_value=2, max_value=5))
    a_rows = tuple((k, draw(st.integers(min_value=-2, max_value=3))) for k in range(n))
    # A strict subset of a's keys match, so at least one a row is unmatched and the WHERE's
    # effect on padded rows is observable.
    matched = draw(st.lists(st.integers(min_value=0, max_value=n - 1), unique=True, max_size=n - 1))
    b_rows = tuple((k, draw(st.integers(min_value=-2, max_value=3))) for k in matched)
    threshold = draw(st.integers(min_value=-2, max_value=3))
    return WhereData(a_rows, b_rows, threshold)


@pytest.fixture(scope="session")
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize("wrap", _WHERE_WRAPS, ids=lambda w: w.replace("{cmp}", "cmp"))
@given(d=_where_data())
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_where_clear_implies_unmatched_rows_survive(
    con: duckdb.DuckDBPyConnection, wrap: str, d: WhereData
) -> None:
    """If the detector clears the predicate, the LEFT JOIN keeps every unmatched row when the
    query actually runs. The data is the judge, so clearing a predicate that silently drops
    unmatched rows -- including a ``coalesce(cmp, true)`` a ``NOT`` or a ``= false`` turns back
    into a drop -- fails here against ground truth rather than against a re-derivation."""
    predicate = wrap.replace("{cmp}", f"b.v > {d.threshold}")
    model_sql = (
        "select a.k as ak, a.v as av, b.k as bk, b.v as bv "
        f"from a left join b on a.k = b.k where {predicate}"
    )
    cleared = (
        len(detect_where_on_outer_joined_nullable(sqlglot.parse_one(model_sql, read="duckdb"))) == 0
    )
    tables = [("a", ("k", "v"), d.a_rows), ("b", ("k", "v"), d.b_rows)]
    with materialized(con, tables, model_sql) as c:
        dropped_unmatched = scalar(
            c,
            "select count(*) from a where a.k not in (select k from b) "
            "and a.k not in (select ak from _m where bk is null)",
        )
    if cleared:
        assert dropped_unmatched == 0, (
            f"detector cleared but {dropped_unmatched} unmatched rows were dropped by "
            f"where {predicate!r} (a={d.a_rows}, b={d.b_rows})"
        )
