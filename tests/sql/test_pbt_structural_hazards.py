"""Property-based tests for two structural detectors, complementing the example tests
in ``test_patterns.py``.

Properties with teeth for the precision fixes these detectors carry:

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

* **Spelling invariance of GROUP BY targets** (parse-only, metamorphic): a query grouped by
  expression and the same query grouped by ordinal or by output alias must draw the same
  findings. The detectors read grouping keys off the ``Group`` node, where both indirect
  spellings arrive as something other than the projected expression, so this pins every
  GROUP BY reader to the semantics rather than to the surface form.

* **The unordered-window verdict against permutation stability** (duckdb execution oracle): the
  detector clears a ranking window exactly when its ORDER BY survives feeding the same rows in a
  different insertion order. A literal inside a window is a constant rather than a positional
  reference, so ``over (order by 1)`` pins nothing, and the engine is the judge of that.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

import duckdb
import pytest
import sqlglot
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.sql.patterns import (
    detect_inner_flatten_row_drop,
    detect_unordered_window,
    detect_where_on_outer_joined_nullable,
    scan_all,
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


# --- spelling invariance of GROUP BY targets ---------------------------------------------


@dataclass(frozen=True)
class _GroupTarget:
    """One projection that a GROUP BY can name, either by expression or by ordinal."""

    sql: str
    alias: str | None

    def projection(self) -> str:
        return self.sql if self.alias is None else f"{self.sql} as {self.alias}"


# Projections chosen so the grouped set reaches every clause the GROUP BY readers branch on:
# the nullable side of the join (the null-group hazard), the preserved side and a
# preserved-side COALESCE fallback (its guards), a two-nullable-side COALESCE (which the
# guards must *not* clear), an IS NOT NULL test (the boolean-bucket clear), and a
# non-deterministic call (the load-bearing-position hazard).
_GROUP_TARGET_SQL = (
    "a.k",
    "b.k",
    "b.status",
    "coalesce(b.k, a.k)",
    "coalesce(b.k, b.status)",
    "b.k is not null",
    "date_diff('day', a.ts, now())",
)


@st.composite
def _group_by_query(draw: st.DrawFn) -> tuple[str, str]:
    """One logical query rendered twice: grouping wholly by expression, and grouping with at
    least one target named indirectly, by its ordinal or by its output alias.

    All three spellings denote the same grouping, so every detector must return the same verdict
    for them. Aliases are drawn independently of the grouping, so an ordinal has to resolve
    through an ``AS`` binding as often as not, and the spelling is drawn per target rather than
    all-or-nothing, so a clause mixing spellings is an ordinary example rather than a special
    case. The alias pool (``p0``..``p3``) deliberately avoids the source column names, since a
    name that could bind to an input column is one this resolution declines to touch.
    """
    chosen = draw(st.lists(st.sampled_from(_GROUP_TARGET_SQL), min_size=1, max_size=4, unique=True))
    targets = [
        _GroupTarget(sql, f"p{i}" if draw(st.booleans()) else None) for i, sql in enumerate(chosen)
    ]
    grouped = draw(
        st.lists(st.integers(min_value=0, max_value=len(targets) - 1), min_size=1, unique=True)
    )
    spellings = [draw(st.sampled_from(_spellings_for(targets[i]))) for i in grouped]
    # At least one target has to be written indirectly, or the two renderings are the same query.
    forced = draw(st.integers(min_value=0, max_value=len(grouped) - 1))
    indirect = [s for s in _spellings_for(targets[grouped[forced]]) if s != "expression"]
    spellings[forced] = draw(st.sampled_from(indirect))
    # The aggregate trails the grouped projections so ordinals index a stable prefix.
    projections = ", ".join([t.projection() for t in targets] + ["sum(a.amt) as total"])
    body = f"select {projections} from a left join b on a.k = b.k group by "
    return (
        body + ", ".join(targets[i].sql for i in grouped),
        body
        + ", ".join(_render(targets[i], i, s) for i, s in zip(grouped, spellings, strict=True)),
    )


def _spellings_for(target: _GroupTarget) -> list[str]:
    """How this target can be named in a GROUP BY: always by its expression or its ordinal, and
    by its output alias when it carries one."""
    return ["expression", "ordinal"] + (["alias"] if target.alias is not None else [])


def _render(target: _GroupTarget, index: int, spelling: str) -> str:
    if spelling == "ordinal":
        return str(index + 1)
    if spelling == "alias":
        assert target.alias is not None
        return target.alias
    return target.sql


@given(q=_group_by_query())
@settings(max_examples=200, deadline=None)
def test_group_by_ordinal_matches_named_spelling(q: tuple[str, str]) -> None:
    """``GROUP BY 1`` is the same query as ``GROUP BY <first projection>``, so it must draw the
    same findings. A detector that reads the ``Group`` node structurally sees an ``exp.Literal``
    where the semantics are the projected expression, which silently disarms it; this property
    is what pins the two spellings together across every detector at once, including the
    off-by-one an ordinal resolved against the wrong projection would introduce."""
    named, positional = q
    assert _scan_kinds(named) == _scan_kinds(positional), (
        f"named {named!r} and positional {positional!r} disagree"
    )


def _scan_kinds(sql: str) -> list[str]:
    return sorted(f.kind.value for f in scan_all(sqlglot.parse_one(sql, read="duckdb")))


# --- a constant ORDER BY is not an ordering (duckdb execution oracle) ---------------------


_WINDOW_ORDERINGS = [
    "order by n",
    "order by 1",
    "order by 'x'",
    "order by null",
    "order by 1 + 2",
    "order by 1, n",
    "order by n + 1",
    "order by -n",
]


@pytest.mark.parametrize("clause", _WINDOW_ORDERINGS)
def test_unordered_window_verdict_matches_permutation_stability(
    con: duckdb.DuckDBPyConnection, clause: str
) -> None:
    """The detector clears a ranking window exactly when its ORDER BY really orders the rows.

    A literal in a *window* ORDER BY is a constant, not the positional reference the same
    literal denotes in a statement-level ORDER BY, so every row sorts equal and the ranking
    falls back to whatever physical order the engine had. The oracle for "really orders" is
    therefore permutation stability: feed the same rows in two different insertion orders and
    see whether the row-number assignment survives. The data is the judge, so a clause we clear
    that does not actually pin an order fails here rather than against a re-derivation.
    """
    ranked: list[list[tuple[int, int]]] = []
    for rows in ("(3), (1), (2)", "(2), (1), (3)"):
        con.execute("create or replace table w(n integer)")
        con.execute(f"insert into w values {rows}")
        assigned = cast(
            "list[tuple[int, int]]",
            con.execute(f"select n, row_number() over ({clause}) rn from w").fetchall(),
        )
        ranked.append(sorted(assigned))
    stable = ranked[0] == ranked[1]
    cleared = not detect_unordered_window(
        sqlglot.parse_one(f"select row_number() over ({clause}) from w", read="duckdb")
    )
    assert cleared == stable, (
        f"detector {'cleared' if cleared else 'flagged'} `{clause}` but the ranking is "
        f"{'stable' if stable else 'unstable'} under input permutation: {ranked}"
    )
