"""Tests for SQL pattern queries and structural hazard detectors."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from dblect.sql import (
    Finding,
    FindingKind,
    JoinSide,
    ParsedSQL,
    detect_coalesce_on_join_key,
    detect_null_group_after_outer_join,
    detect_unordered_aggregate,
    detect_unordered_window,
    list_aggregations,
    list_group_bys,
    list_joins,
    list_windows,
    scan_all,
)


def _parse(sql: str) -> ParsedSQL:
    return ParsedSQL.parse(sql, dialect="duckdb")


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
