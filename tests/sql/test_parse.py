"""Tests for SQL parsing."""

from __future__ import annotations

import pytest
import sqlglot.expressions as exp
from hypothesis import given, settings
from hypothesis import strategies as st

from dblect.sql import SQLParseError, parse_sql


def test_parses_plain_sql() -> None:
    tree = parse_sql("select a, b from t", dialect="duckdb")
    assert isinstance(tree, exp.Select)


def test_unparseable_sql_raises_typed_error() -> None:
    with pytest.raises(SQLParseError) as excinfo:
        parse_sql("select from where", dialect="duckdb")
    assert excinfo.value.sql == "select from where"


_RESERVED = frozenset(
    {"select", "from", "where", "on", "as", "and", "or", "not", "in", "is", "null"}
)
_IDENT = st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True).filter(
    lambda s: s not in _RESERVED
)


@given(
    cols=st.lists(_IDENT, min_size=1, max_size=5, unique=True),
    table=_IDENT,
)
@settings(max_examples=50, deadline=None)
def test_simple_select_round_trips_through_parse(cols: list[str], table: str) -> None:
    sql = f"select {', '.join(cols)} from {table}"
    tree = parse_sql(sql, dialect="duckdb")
    rendered = tree.sql(dialect="duckdb")
    tree2 = parse_sql(rendered, dialect="duckdb")
    # Idempotent at the AST level after one round-trip.
    assert tree2.sql(dialect="duckdb") == rendered
