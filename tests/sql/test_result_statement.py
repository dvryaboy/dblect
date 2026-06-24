"""Splitting a compiled model into its result statement.

A compiled model can be a script: leading DDL (``CREATE FUNCTION``), session
state (``DECLARE``, ``SET``) before the terminal ``SELECT``. The analysis layer
needs the result statement, the final top-level query, and treats the leading
statements as a non-result prelude. A script with more than one result-producing
statement cannot be reduced to a single model and is a coverage miss, never a
guess.
"""

from __future__ import annotations

import pytest
import sqlglot.expressions as exp
from hypothesis import given, settings
from hypothesis import strategies as st

from dblect.sql import (
    MultiResultScript,
    NoResultScript,
    SingleResult,
    parse_result_statement,
    parse_sql,
)


def test_bare_select_is_a_single_result() -> None:
    outcome = parse_result_statement("SELECT a, b FROM t", dialect="duckdb")
    assert isinstance(outcome, SingleResult)
    assert isinstance(outcome.statement, exp.Select)


def test_two_result_statements_is_a_multi_result_miss() -> None:
    outcome = parse_result_statement("SELECT a FROM t;\nSELECT b FROM u", dialect="duckdb")
    assert isinstance(outcome, MultiResultScript)
    assert outcome.result_count == 2


@pytest.mark.parametrize("body", ["CREATE TABLE t (x INT)", "INSERT INTO t (x) VALUES (1)"])
def test_body_with_no_query_is_a_no_result_miss(body: str) -> None:
    # A VALUES-only or DDL-only compiled body has no query to follow lineage on.
    assert isinstance(parse_result_statement(body, dialect="duckdb"), NoResultScript)


@pytest.mark.parametrize(
    "wrapped",
    [
        "CREATE TABLE foo AS SELECT a, b FROM t",
        "CREATE VIEW foo AS SELECT a, b FROM t",
        "INSERT INTO foo SELECT a, b FROM t",
    ],
)
def test_materialization_wrapper_reduces_to_its_inner_select(wrapped: str) -> None:
    # A CTAS / CREATE VIEW AS / INSERT...SELECT carries its logic in the inner SELECT;
    # the reducer hands that query to the detectors, identical to the bare SELECT.
    outcome = parse_result_statement(wrapped, dialect="duckdb")
    assert isinstance(outcome, SingleResult)
    bare = parse_sql("SELECT a, b FROM t", dialect="duckdb")
    assert outcome.statement.sql(dialect="duckdb") == bare.sql(dialect="duckdb")


def test_parse_sql_tolerates_a_ddl_prelude() -> None:
    # parse_sql is the single-statement door the detectors use; it should now see
    # through a DDL prelude to the result SELECT rather than analysing the prelude.
    bare = parse_sql("SELECT a, b FROM t", dialect="duckdb")
    prefixed = parse_sql(
        "CREATE TEMPORARY FUNCTION g(x INT) AS (x);\nSELECT a, b FROM t",
        dialect="duckdb",
    )
    assert prefixed.sql(dialect="duckdb") == bare.sql(dialect="duckdb")


# Prefix every generated name with ``id_`` so it can never collide with a SQL keyword
# (``in``, ``on``, ``select``, ...); a keyword used as a function or table name is a
# parse error in its own right and is not what this property is exercising.
_IDENT = st.from_regex(r"[a-z][a-z0-9_]{0,6}", fullmatch=True).map(lambda s: f"id_{s}")


@given(
    cols=st.lists(_IDENT, min_size=1, max_size=4, unique=True),
    table=_IDENT,
    fn=_IDENT,
)
@settings(max_examples=60, deadline=None)
def test_ddl_prelude_does_not_change_the_result_statement(
    cols: list[str], table: str, fn: str
) -> None:
    """A leading inline-function definition never changes the result statement the
    parser hands back: the SELECT parses identically with or without the prelude."""
    bare_sql = f"SELECT {', '.join(cols)} FROM {table}"
    prelude = f"CREATE TEMPORARY FUNCTION {fn}(x INT) AS (x + 1);"
    bare = parse_result_statement(bare_sql, dialect="duckdb")
    scripted = parse_result_statement(f"{prelude}\n{bare_sql}", dialect="duckdb")
    assert isinstance(bare, SingleResult)
    assert isinstance(scripted, SingleResult)
    assert scripted.statement.sql(dialect="duckdb") == bare.statement.sql(dialect="duckdb")
