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
    p = ParsedSQL.parse(sql, dialect="duckdb")
    rendered = p.tree.sql(dialect="duckdb")
    p2 = ParsedSQL.parse(rendered, dialect="duckdb")
    # Idempotent at the AST level after one round-trip.
    assert p2.tree.sql(dialect="duckdb") == rendered


def test_redaction_preserves_line_count_for_inline_jinja() -> None:
    sql = "select {{ ref('x') }} from t"
    p = ParsedSQL.parse(sql, dialect="duckdb")
    assert p.redacted.count("\n") == sql.count("\n")


def test_redaction_preserves_line_count_for_multiline_expr() -> None:
    sql = (
        "select\n"
        "  a,\n"
        "  {{ var('y',\n"
        "         'default') }} as y,\n"
        "  c\n"
        "from t\n"
    )
    p = ParsedSQL.parse(sql, dialect="duckdb")
    assert p.redacted.count("\n") == sql.count("\n")
    # `from t` was on line 6 originally; it should still be on line 6 after redaction.
    redacted_lines = p.redacted.splitlines()
    assert redacted_lines[5].strip().startswith("from t")


def test_redaction_preserves_line_count_for_multiline_statement_tag() -> None:
    sql = (
        "{% set methods = [\n"
        "    'a',\n"
        "    'b',\n"
        "] %}\n"
        "select x from t\n"
    )
    p = ParsedSQL.parse(sql, dialect="duckdb")
    assert p.redacted.count("\n") == sql.count("\n")
    redacted_lines = p.redacted.splitlines()
    assert redacted_lines[4].strip().startswith("select x")


def test_redaction_preserves_line_count_for_multiline_comment() -> None:
    sql = (
        "{# big\n"
        "   block\n"
        "   comment #}\n"
        "select 1\n"
    )
    p = ParsedSQL.parse(sql, dialect="duckdb")
    assert p.redacted.count("\n") == sql.count("\n")


_JINJA_BLOCK = st.sampled_from(
    [
        "{# comment #}",
        "{#\n  multi-line\n  comment #}",
        "{% set x = 1 %}",
        "{%\n  set x = 1\n%}",
        "{{ ref('m') }}",
        "{{ source('s', 't') }}",
        "{{\n  var('y',\n      'default')\n}}",
    ]
)


@given(
    blocks=st.lists(_JINJA_BLOCK, min_size=0, max_size=6),
    pre=st.sampled_from(["", "select 1\n", "select x from t\n"]),
    post=st.sampled_from(["", " from t", " from t\n", "\nfrom t\n"]),
)
@settings(max_examples=60, deadline=None)
def test_redaction_preserves_line_count_pbt(
    blocks: list[str], pre: str, post: str
) -> None:
    sql = pre + " ".join(blocks) + post
    # The PBT compositions sometimes produce SQL that can't actually be parsed
    # (e.g., an expression where a clause is expected); in those cases we still
    # want to assert the redaction step's line-preservation invariant, which is
    # what we're testing here. So bypass `ParsedSQL.parse` and exercise
    # `_redact_jinja` directly.
    from dblect.sql.parse import _redact_jinja  # pyright: ignore[reportPrivateUsage]

    redacted, _ = _redact_jinja(sql)
    assert redacted.count("\n") == sql.count("\n")


@given(
    n=st.integers(min_value=0, max_value=6),
)
@settings(max_examples=30, deadline=None)
def test_ref_count_matches_jinja_ref_occurrences(n: int) -> None:
    refs = [f"m{i}" for i in range(n)]
    if refs:
        clauses = " union all ".join(f"select 1 as x from {{{{ ref('{r}') }}}}" for r in refs)
        sql = f"with cte as ({clauses}) select * from cte"
    else:
        sql = "select 1"
    p = ParsedSQL.parse(sql, dialect="duckdb")
    assert p.refs == tuple(refs)
