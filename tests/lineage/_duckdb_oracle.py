"""A duckdb materialization oracle for lineage soundness PBTs.

The empirical soundness PBTs (uniqueness keys, nullability) share one move: build
the generated sources in duckdb, materialize the model's SQL against them, and
query the result as ground truth. This helper is that move, factored out so each
PBT only writes its generator and its assertion. The oracle is the data, so a
property the analyzer claims is checked against what the warehouse actually
produces, not against a re-derivation of the rule.

Columns are integer-typed (the generators produce small non-null ints); extend the
DDL here if a future PBT needs another type.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager

import duckdb

# A source table to materialize: its name, its column names, and its rows.
Table = tuple[str, tuple[str, ...], Sequence[Sequence[object]]]


@contextmanager
def materialized(
    con: duckdb.DuckDBPyConnection, tables: Sequence[Table], model_sql: str
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Create ``tables`` on ``con``, materialize ``model_sql`` as ``_m``, and yield the
    connection for the caller to query.

    The connection is shared across Hypothesis examples (the ``oracle_con`` fixture), so
    tables are created with ``CREATE OR REPLACE`` and dropped on exit: each example sees
    a clean slate without paying to open a fresh in-memory database, which dominated
    these PBTs' runtime.
    """
    created: list[str] = []
    try:
        for name, cols, rows in tables:
            con.execute(
                f"CREATE OR REPLACE TABLE {name} ({', '.join(f'{c} INTEGER' for c in cols)})"
            )
            created.append(name)
            if rows:
                placeholders = ", ".join(["?"] * len(cols))
                con.executemany(
                    f"INSERT INTO {name} VALUES ({placeholders})", [list(r) for r in rows]
                )
        con.execute(f"CREATE OR REPLACE TABLE _m AS {model_sql}")
        created.append("_m")
        yield con
    finally:
        for name in reversed(created):
            con.execute(f"DROP TABLE IF EXISTS {name}")


def scalar(con: duckdb.DuckDBPyConnection, query: str) -> int:
    """Run ``query`` (expected to return one integer) and return it."""
    row = con.execute(query).fetchone()
    assert row is not None
    return int(row[0])


def assert_no_over_claims(
    con: duckdb.DuckDBPyConnection,
    tables: Sequence[Table],
    model_sql: str,
    violations: Callable[[duckdb.DuckDBPyConnection], Mapping[str, int]],
) -> None:
    """Materialize ``tables`` and ``model_sql``, then assert every count ``violations``
    reports is zero: the "annotation over-approximates the materialized truth" move
    every empirical soundness PBT makes (a NON_NULL column with a null row, a claimed
    key with duplicate tuples), whatever it is counting.

    ``violations`` runs against the live materialization and returns one count per
    claim, labelled for the assertion message; a property supplies only that (its
    generator and grammar live in its own test, and how it counts a violation is
    property-specific), not the materialize/teardown dance around it.
    """
    with materialized(con, tables, model_sql) as c:
        counts = violations(c)
    bad = {label: n for label, n in counts.items() if n != 0}
    assert not bad, f"over-claimed: {bad} for sql={model_sql!r} tables={tables!r}"
