"""Soundness of the ``ROW_NUMBER`` conditional key against materialized rows.

A projected ``ROW_NUMBER() OVER (PARTITION BY p ...) AS rn`` carries ``p`` as a key
under ``rn <= 1``. Whatever filter site and predicate a consumer uses, every key the
analyzer then promotes must be unique over the rows duckdb produces. The generator
draws the join kind, where the rank filter sits, the predicate (activating and not),
the partition, and rows with NULLs, and never states which keys to expect: the data is
the judge. ``test_uniqueness_rownumber_conditional`` pins which combinations activate.
"""

from __future__ import annotations

import duckdb
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.lineage._duckdb_oracle import Table, assert_keys_unique
from tests.lineage._rownumber_keys import model, promoted_keys

_JOINS = ("JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN")
_SITES = ("on", "where", "none")
_PREDICATES = (
    "f.rn = 1",
    "1 = f.rn",
    "f.rn <= 1",
    "f.rn < 2",
    "f.rn IN (1)",
    "f.rn = 2",
    "f.rn <= 2",
    "f.rn > 1",
    "f.rn IS NULL",
    "f.rn = 1 OR d.c0 = 0",
    "NOT f.rn > 1",
    "f.rn = 1 AND f.c2 > 0",
)
_PARTITIONS = ("c1", "c1, c2")
_VALUE = st.one_of(st.none(), st.integers(min_value=0, max_value=3))
_ROWS = st.lists(st.tuples(_VALUE, _VALUE, _VALUE), max_size=12)


def _sql(join: str, site: str, predicate: str, partition: str) -> str:
    on = "d.c0 = f.c1" + (f" AND {predicate}" if site == "on" else "")
    where = f" WHERE {predicate}" if site == "where" else ""
    return (
        f"WITH f AS (SELECT c0, c1, c2, ROW_NUMBER() OVER "
        f"(PARTITION BY {partition} ORDER BY c0) AS rn FROM events), "
        "d AS (SELECT DISTINCT c0 FROM events) "
        f"SELECT d.c0, f.c1, f.c2 FROM d {join} f ON {on}{where}"
    )


@given(
    join=st.sampled_from(_JOINS),
    site=st.sampled_from(_SITES),
    predicate=st.sampled_from(_PREDICATES),
    partition=st.sampled_from(_PARTITIONS),
    rows=_ROWS,
)
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_promoted_keys_are_unique_over_the_materialized_rows(
    oracle_con: duckdb.DuckDBPyConnection,
    join: str,
    site: str,
    predicate: str,
    partition: str,
    rows: list[tuple[int | None, int | None, int | None]],
) -> None:
    sql = _sql(join, site, predicate, partition)
    keys = promoted_keys("m", model("m", sql))
    tables: list[Table] = [("events", ("c0", "c1", "c2"), rows)]
    assert_keys_unique(oracle_con, tables, sql, keys)


_LATER_JOINS = ("JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN")
_CHAINS = (
    # the rank filter rides a later join's ON, against the FROM source or an earlier-joined one
    "f {later} d ON f.c1 = d.c0 AND {predicate}",
    "d JOIN f ON d.c0 = f.c1 {later} e ON e.c0 = d.c0 AND {predicate}",
    "d LEFT JOIN f ON d.c0 = f.c1 {later} e ON e.c0 = d.c0 AND {predicate}",
    "f JOIN e ON e.c0 = f.c1 {later} d ON d.c0 = e.c0 AND {predicate}",
)


@given(
    chain=st.sampled_from(_CHAINS),
    later=st.sampled_from(_LATER_JOINS),
    predicate=st.sampled_from(_PREDICATES),
    partition=st.sampled_from(_PARTITIONS),
    rows=_ROWS,
)
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_keys_promoted_across_a_later_joins_on_are_unique_over_the_materialized_rows(
    oracle_con: duckdb.DuckDBPyConnection,
    chain: str,
    later: str,
    predicate: str,
    partition: str,
    rows: list[tuple[int | None, int | None, int | None]],
) -> None:
    sql = (
        f"WITH f AS (SELECT c0, c1, c2, ROW_NUMBER() OVER "
        f"(PARTITION BY {partition} ORDER BY c0) AS rn FROM events), "
        "d AS (SELECT DISTINCT c0 FROM events), e AS (SELECT DISTINCT c0 FROM events) "
        f"SELECT d.c0, f.c1, f.c2 FROM {chain.format(later=later, predicate=predicate)}"
    )
    keys = promoted_keys("m", model("m", sql))
    tables: list[Table] = [("events", ("c0", "c1", "c2"), rows)]
    assert_keys_unique(oracle_con, tables, sql, keys)
