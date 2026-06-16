"""Tests for SQL pattern queries and structural hazard detectors."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlglot import Expr

from dblect.sql import (
    Finding,
    FindingKind,
    JoinSide,
    detect_coalesce_on_join_key,
    detect_null_group_after_outer_join,
    detect_unordered_aggregate,
    detect_unordered_window,
    detect_where_on_outer_joined_nullable,
    list_aggregations,
    list_group_bys,
    list_joins,
    list_windows,
    make_non_determinism_detector,
    parse_sql,
    scan_all,
)


def _parse(sql: str) -> Expr:
    return parse_sql(sql, dialect="duckdb")


def _non_determinism(sql: str, *, builtins: frozenset[str] | None = None) -> tuple[Finding, ...]:
    detector = (
        make_non_determinism_detector()
        if builtins is None
        else make_non_determinism_detector(builtins)
    )
    return detector(_parse(sql))


def _kinds(findings: tuple[Finding, ...]) -> set[FindingKind]:
    return {f.kind for f in findings}


def test_list_joins_captures_side_and_tables() -> None:
    p = _parse("select * from a left join b on a.x = b.x inner join c on a.y = c.y")
    joins = list_joins(p)
    assert [j.side for j in joins] == [JoinSide.LEFT, JoinSide.INNER]
    assert [(j.left_table, j.right_table) for j in joins] == [("a", "b"), ("b", "c")]


def test_list_windows_partitions_ranking_vs_non_ranking() -> None:
    p = _parse("select row_number() over (order by x) as rn, sum(z) over () as s from t")
    ws = list_windows(p)
    assert {w.function for w in ws} == {"RowNumber", "Sum"}
    ranking = next(w for w in ws if w.function == "RowNumber")
    assert ranking.is_ranking is True
    # The rendered form may carry NULLS positioning depending on sqlglot version;
    # we only care that the order key is present and non-empty.
    assert len(ranking.order_by) == 1
    assert ranking.order_by[0].startswith("x")
    non_ranking = next(w for w in ws if w.function == "Sum")
    assert non_ranking.is_ranking is False
    assert non_ranking.order_by == ()


def test_list_group_bys_one_per_select() -> None:
    p = _parse("with a as (select x from t group by x) select y from a group by y")
    groups = list_group_bys(p)
    assert len(groups) == 2
    assert {g.targets for g in groups} == {("x",), ("y",)}


def test_list_aggregations_excludes_windowed_functions() -> None:
    p = _parse("select sum(x), sum(y) over () from t")
    aggs = list_aggregations(p)
    assert len(aggs) == 1
    assert aggs[0].function == "Sum"
    assert aggs[0].argument_sql == "x"


def test_null_group_after_left_join_detected() -> None:
    sql = """
    select b.k, sum(amount) as total
    from a left join b on a.k = b.k
    group by b.k
    """
    findings = detect_null_group_after_outer_join(_parse(sql))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN


def test_null_group_after_inner_join_not_detected() -> None:
    sql = """
    select b.k, sum(amount) as total
    from a inner join b on a.k = b.k
    group by b.k
    """
    findings = detect_null_group_after_outer_join(_parse(sql))
    assert findings == ()


def test_null_group_after_left_join_group_by_left_side_not_detected() -> None:
    sql = """
    select a.k, sum(amount) as total
    from a left join b on a.k = b.k
    group by a.k
    """
    findings = detect_null_group_after_outer_join(_parse(sql))
    assert findings == ()


def test_null_group_after_right_join_flips_nullability() -> None:
    sql = """
    select a.k, count(*) as n
    from a right join b on a.k = b.k
    group by a.k
    """
    findings = detect_null_group_after_outer_join(_parse(sql))
    assert len(findings) == 1


def test_coalesce_on_join_key_detected() -> None:
    sql = """
    select coalesce(a.k, 0) as k_safe
    from a left join b on a.k = b.k
    """
    findings = detect_coalesce_on_join_key(_parse(sql))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.COALESCE_ON_JOIN_KEY


def test_coalesce_on_non_join_column_not_detected() -> None:
    sql = """
    select coalesce(a.name, '') as name
    from a left join b on a.id = b.id
    """
    findings = detect_coalesce_on_join_key(_parse(sql))
    assert findings == ()


def test_unordered_row_number_detected() -> None:
    p = _parse("select row_number() over (partition by x) as rn from t")
    findings = detect_unordered_window(p)
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNORDERED_RANKING_WINDOW


def test_ordered_row_number_not_flagged() -> None:
    p = _parse("select row_number() over (order by ts) as rn from t")
    assert detect_unordered_window(p) == ()


@pytest.mark.parametrize(
    "func",
    ["lag(x)", "lead(x)", "first_value(x)", "last_value(x)", "nth_value(x, 2)"],
)
def test_unordered_lookup_window_functions_are_detected(func: str) -> None:
    p = _parse(f"select {func} over (partition by g) from t")
    findings = detect_unordered_window(p)
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNORDERED_RANKING_WINDOW


@pytest.mark.parametrize(
    "func",
    ["lag(x)", "lead(x)", "first_value(x)", "last_value(x)"],
)
def test_ordered_lookup_window_functions_not_flagged(func: str) -> None:
    p = _parse(f"select {func} over (partition by g order by ts) from t")
    assert detect_unordered_window(p) == ()


def test_unordered_array_agg_detected() -> None:
    p = _parse("select array_agg(x) as arr from t")
    findings = detect_unordered_aggregate(p)
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNORDERED_AGGREGATE


def test_ordered_array_agg_not_flagged() -> None:
    p = _parse("select array_agg(x order by y) as arr from t")
    assert detect_unordered_aggregate(p) == ()


def test_within_group_array_agg_not_flagged() -> None:
    p = _parse("select array_agg(x) within group (order by y) as arr from t")
    assert detect_unordered_aggregate(p) == ()


def test_unordered_string_agg_detected() -> None:
    # sqlglot parses both STRING_AGG and GROUP_CONCAT into exp.GroupConcat,
    # so the existing aggregate detector covers them.
    p = _parse("select string_agg(name, ',') as names from t")
    findings = detect_unordered_aggregate(p)
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNORDERED_AGGREGATE


def test_ordered_string_agg_not_flagged() -> None:
    p = _parse("select string_agg(name, ',' order by ts) as names from t")
    assert detect_unordered_aggregate(p) == ()


def test_unordered_group_concat_detected() -> None:
    p = _parse("select group_concat(name) as names from t")
    findings = detect_unordered_aggregate(p)
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNORDERED_AGGREGATE


def test_finding_carries_line_range_of_offending_expression() -> None:
    sql = (
        "select b.k,\n"  # line 1
        "       sum(amount) as total\n"  # line 2
        "from a\n"  # line 3
        "left join b on a.k = b.k\n"  # line 4
        "group by b.k\n"  # line 5
    )
    findings = detect_null_group_after_outer_join(_parse(sql))
    assert len(findings) == 1
    # The flagged GROUP BY expression is on line 5.
    assert findings[0].line_start == 5
    assert findings[0].line_end == 5


# --- WHERE on outer-joined nullable ---


def test_where_on_left_joined_nullable_detected() -> None:
    sql = "select * from a left join b on a.k = b.k where b.status = 'active'"
    findings = detect_where_on_outer_joined_nullable(_parse(sql))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.WHERE_ON_OUTER_JOINED_NULLABLE


def test_where_on_left_joined_left_side_not_detected() -> None:
    # a is the left side; predicates on a.* don't invert the join.
    sql = "select * from a left join b on a.k = b.k where a.status = 'active'"
    assert detect_where_on_outer_joined_nullable(_parse(sql)) == ()


def test_where_on_inner_joined_not_detected() -> None:
    sql = "select * from a inner join b on a.k = b.k where b.status = 'active'"
    assert detect_where_on_outer_joined_nullable(_parse(sql)) == ()


def test_where_is_null_on_nullable_not_detected() -> None:
    # IS NULL on the nullable side is the explicit "find unmatched rows" idiom.
    sql = "select * from a left join b on a.k = b.k where b.k is null"
    assert detect_where_on_outer_joined_nullable(_parse(sql)) == ()


def test_where_coalesced_nullable_not_detected() -> None:
    sql = "select * from a left join b on a.k = b.k where coalesce(b.status, 'unknown') = 'active'"
    assert detect_where_on_outer_joined_nullable(_parse(sql)) == ()


def test_where_in_predicate_on_nullable_detected() -> None:
    sql = "select * from a left join b on a.k = b.k where b.status in ('x', 'y')"
    assert len(detect_where_on_outer_joined_nullable(_parse(sql))) == 1


def test_where_between_on_nullable_detected() -> None:
    sql = "select * from a left join b on a.k = b.k where b.amount between 1 and 10"
    assert len(detect_where_on_outer_joined_nullable(_parse(sql))) == 1


def test_where_on_right_joined_left_side_detected() -> None:
    sql = "select * from a right join b on a.k = b.k where a.status = 'x'"
    assert len(detect_where_on_outer_joined_nullable(_parse(sql))) == 1


# --- Non-deterministic function in load-bearing positions ---


def test_now_in_join_on_detected() -> None:
    sql = "select * from a join b on a.k = b.k and b.created_at < now()"
    findings = _non_determinism(sql)
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NON_DETERMINISTIC_FUNCTION


def test_now_in_group_by_detected() -> None:
    sql = "select date_diff('day', ts, now()) as days_ago, count(*) from t group by 1"
    # `group by 1` is a positional reference; the GROUP BY *target* in the AST
    # is the literal 1, not the expression. So this won't fire. The pattern
    # we care about is `group by <expression containing now()>` directly.
    # (Documented because someone will inevitably wonder why it didn't trigger.)
    assert _non_determinism(sql) == ()


def test_now_in_explicit_group_by_expression_detected() -> None:
    sql = (
        "select date_diff('day', ts, now()) as days_ago, count(*) "
        "from t group by date_diff('day', ts, now())"
    )
    findings = _non_determinism(sql)
    assert len(findings) == 1


def test_current_timestamp_in_window_order_by_detected() -> None:
    sql = "select row_number() over (order by ts - current_timestamp) as rn from t"
    findings = _non_determinism(sql)
    assert len(findings) == 1


def test_random_in_window_partition_by_detected() -> None:
    sql = "select rank() over (partition by random() order by ts) from t"
    findings = _non_determinism(sql)
    assert len(findings) == 1


def test_now_in_where_not_detected() -> None:
    # The lookback idiom we explicitly want to leave alone.
    sql = "select * from t where ts >= now() - interval '7 days'"
    assert _non_determinism(sql) == ()


def test_now_in_projection_not_detected() -> None:
    # Audit columns are common and benign.
    sql = "select x, current_timestamp as loaded_at from t"
    assert _non_determinism(sql) == ()


def test_uuid_in_join_on_detected() -> None:
    sql = "select * from a join b on a.k = gen_random_uuid()"
    findings = _non_determinism(sql)
    assert len(findings) == 1


def test_now_function_call_alias_detected() -> None:
    # now() arrives as exp.Anonymous; current_timestamp is a typed node.
    sql = "select * from t group by now()"
    findings = _non_determinism(sql)
    assert len(findings) == 1
    assert "now" in findings[0].message.lower()


def test_anonymous_builtin_fires_only_when_in_the_passed_set() -> None:
    # txid_current() parses as exp.Anonymous in every dialect, so the name set handed
    # to the factory is the only thing that decides whether it fires. It is absent from
    # the portable baseline, so the default detector stays silent; an adapter that adds
    # it (DuckDB) catches it. This is the contract the per-adapter set rides on.
    sql = "select * from a join b on a.k = txid_current()"
    assert _non_determinism(sql) == ()
    assert len(_non_determinism(sql, builtins=frozenset({"txid_current"}))) == 1


def test_baseline_builtin_fires_under_the_default_set() -> None:
    # now() is in the portable baseline, so the default detector catches it with no
    # adapter-specific additions.
    sql = "select * from a join b on a.k = b.k and b.created_at < now()"
    assert len(_non_determinism(sql)) == 1


def test_scan_all_runs_every_detector() -> None:
    sql = """
    select coalesce(a.k, 0) as k_safe,
           row_number() over (partition by b.k) as rn,
           array_agg(amount) as amounts
    from a left join b on a.k = b.k
    group by b.k
    """
    findings = scan_all(_parse(sql))
    kinds = _kinds(findings)
    assert FindingKind.NULL_GROUP_AFTER_OUTER_JOIN in kinds
    assert FindingKind.COALESCE_ON_JOIN_KEY in kinds
    assert FindingKind.UNORDERED_RANKING_WINDOW in kinds
    assert FindingKind.UNORDERED_AGGREGATE in kinds


# Property-based invariants:

_SQL_KEYWORDS = frozenset(
    {
        "as",
        "from",
        "where",
        "group",
        "order",
        "by",
        "select",
        "join",
        "on",
        "in",
        "is",
        "and",
        "or",
        "not",
        "null",
        "case",
        "when",
        "then",
        "else",
        "end",
        "all",
        "any",
        "with",
        "having",
        "union",
        "limit",
        "offset",
        "distinct",
        "inner",
        "outer",
        "left",
        "right",
        "full",
        "cross",
        "asc",
        "desc",
        "true",
        "false",
        "to",
        "if",
        "for",
        "do",
        "of",
        "set",
        "table",
        "into",
    }
)

_IDENT = st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True).filter(
    lambda s: s not in _SQL_KEYWORDS
)


@given(cols=st.lists(_IDENT, min_size=1, max_size=4, unique=True), table=_IDENT)
@settings(max_examples=40, deadline=None)
def test_no_joins_implies_no_outer_join_findings(cols: list[str], table: str) -> None:
    """A query with no JOIN clauses cannot produce NULL-group-after-outer-join findings."""
    cols_csv = ", ".join(cols)
    p = _parse(f"select {cols_csv} from {table} group by {cols[0]}")
    assert detect_null_group_after_outer_join(p) == ()
    assert detect_coalesce_on_join_key(p) == ()


@given(table=_IDENT, col=_IDENT)
@settings(max_examples=40, deadline=None)
def test_no_windows_implies_no_window_findings(table: str, col: str) -> None:
    """A query with no window expression cannot produce window-ordering findings."""
    p = _parse(f"select sum({col}) as s from {table}")
    assert detect_unordered_window(p) == ()


@given(
    n=st.integers(min_value=0, max_value=4),
    table=_IDENT,
)
@settings(max_examples=30, deadline=None)
def test_inner_join_chains_never_produce_null_group_findings(n: int, table: str) -> None:
    """Inner joins, however deep, don't put any side in nullable scope."""
    joins = " ".join(f"inner join {table}_{i} on {table}.k = {table}_{i}.k" for i in range(n))
    sql = f"select {table}.k, count(*) from {table} {joins} group by {table}.k"
    p = _parse(sql)
    assert detect_null_group_after_outer_join(p) == ()
