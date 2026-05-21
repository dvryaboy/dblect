"""Tests for SQL parsing and Jinja redaction."""

from __future__ import annotations

import pytest
import sqlglot.expressions as exp
from hypothesis import given, settings
from hypothesis import strategies as st

from dblect.sql import ParsedSQL, PlaceholderKind, SQLParseError


def test_parses_plain_sql() -> None:
    p = ParsedSQL.parse("select a, b from t", dialect="duckdb")
    assert isinstance(p.tree, exp.Select)
    assert p.placeholders == ()
    assert p.refs == ()


def test_redacts_ref_to_target_name() -> None:
    p = ParsedSQL.parse("select * from {{ ref('stg_orders') }}", dialect="duckdb")
    assert p.refs == ("stg_orders",)
    [ph] = p.placeholders
    assert ph.kind is PlaceholderKind.REF
    assert ph.target == "stg_orders"
    assert ph.sentinel == "stg_orders"
    # The redacted SQL should contain the bare identifier.
    assert "stg_orders" in p.redacted
    assert "{{" not in p.redacted


def test_redacts_source_to_compound_sentinel() -> None:
    p = ParsedSQL.parse("select * from {{ source('raw', 'orders') }}", dialect="duckdb")
    [ph] = p.placeholders
    assert ph.kind is PlaceholderKind.SOURCE
    assert ph.target == "raw.orders"
    assert "raw__orders" in p.redacted


def test_strips_jinja_comments() -> None:
    p = ParsedSQL.parse(
        "select {#- silly comment -#} 1 as a {# trailing #} from t",
        dialect="duckdb",
    )
    assert "{#" not in p.redacted
    assert "#}" not in p.redacted


def test_strips_statement_tags_keeping_body() -> None:
    # The for-loop body stays once; the tags are removed.
    sql = """
    {% set methods = ['a', 'b'] %}
    select
      {% for m in methods -%}
        sum(case when method = '{{ m }}' then 1 else 0 end) as {{ m }}_count{% if not loop.last %},{% endif %}
      {% endfor %}
    from t
    """
    p = ParsedSQL.parse(sql, dialect="duckdb")
    assert "{%" not in p.redacted
    assert "%}" not in p.redacted
    # The (one-shot) loop body becomes a single aggregate.
    aggs = list(p.tree.find_all(exp.Sum))
    assert len(aggs) == 1


def test_unparseable_redacted_sql_raises_typed_error() -> None:
    with pytest.raises(SQLParseError) as excinfo:
        ParsedSQL.parse("select from where", dialect="duckdb")
    assert "select from where" in excinfo.value.redacted_sql


@given(
    cols=st.lists(
        st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True),
        min_size=1,
        max_size=5,
        unique=True,
    ),
    table=st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True),
)
@settings(max_examples=50, deadline=None)
def test_simple_select_round_trips_through_parse(cols: list[str], table: str) -> None:
    sql = f"select {', '.join(cols)} from {table}"
    p = ParsedSQL.parse(sql, dialect="duckdb")
    rendered = p.tree.sql(dialect="duckdb")
    p2 = ParsedSQL.parse(rendered, dialect="duckdb")
    # Idempotent at the AST level after one round-trip.
    assert p2.tree.sql(dialect="duckdb") == rendered


@given(
    n=st.integers(min_value=0, max_value=6),
)
@settings(max_examples=30, deadline=None)
def test_ref_count_matches_jinja_ref_occurrences(n: int) -> None:
    refs = [f"m{i}" for i in range(n)]
    if not refs:
        sql = "select 1"
    else:
        joined = ", ".join(f"select * from {{{{ ref('{r}') }}}}" for r in refs)
        sql = f"with cte as ({joined}) select 1"
    # Construct a parseable statement that places each ref in a FROM
    if refs:
        clauses = " union all ".join(f"select 1 as x from {{{{ ref('{r}') }}}}" for r in refs)
        sql = f"with cte as ({clauses}) select * from cte"
    p = ParsedSQL.parse(sql, dialect="duckdb")
    assert p.refs == tuple(refs)
