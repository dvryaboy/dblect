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
    detect_inner_flatten_row_drop,
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


# --- GROUP BY on outer-joined nullable: COALESCE / IS NULL guards (#169) ---


def test_null_group_coalesce_to_preserved_side_not_detected() -> None:
    # base is the preserved (FROM) side, so coalesce(meta.key, base.key) is never NULL.
    sql = """
    select coalesce(meta.key, base.key) as key, sum(base.amount) as amount
    from base
    left join meta on base.key = meta.key
    group by coalesce(meta.key, base.key)
    """
    assert detect_null_group_after_outer_join(_parse(sql)) == ()


def test_null_group_coalesce_to_literal_not_detected() -> None:
    sql = """
    select coalesce(b.k, 'none') as k, count(*) as n
    from a left join b on a.k = b.k
    group by coalesce(b.k, 'none')
    """
    assert detect_null_group_after_outer_join(_parse(sql)) == ()


def test_null_group_coalesce_all_nullable_sides_detected() -> None:
    # Both b and c are nullable (full outer joins), so the merged key can be NULL.
    sql = """
    select coalesce(b.k, c.k) as k, count(*) as n
    from a
    full outer join b on a.k = b.k
    full outer join c on a.k = c.k
    group by coalesce(b.k, c.k)
    """
    findings = detect_null_group_after_outer_join(_parse(sql))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN


def test_null_group_is_not_null_key_not_detected() -> None:
    # Grouping by a boolean IS NOT NULL test has two real buckets, no phantom NULL group.
    sql = """
    select b.k is not null as matched, count(*) as n
    from a left join b on a.k = b.k
    group by b.k is not null
    """
    assert detect_null_group_after_outer_join(_parse(sql)) == ()


# --- WHERE on outer-joined nullable: top-level OR-sibling rescue (#168) ---


def test_where_left_join_or_sibling_on_preserved_side_not_detected() -> None:
    # An unmatched left row (b.* NULL) still survives via a.x > 0, so the OR is
    # join-preserving and neither term inverts the join.
    sql = "select * from a left join b on a.k = b.k where a.x > 0 or b.y > 0"
    assert detect_where_on_outer_joined_nullable(_parse(sql)) == ()


def test_where_full_outer_both_sides_or_not_detected() -> None:
    sql = "select * from l full outer join r on l.k = r.k where l.v > 0 or r.v > 0"
    assert detect_where_on_outer_joined_nullable(_parse(sql)) == ()


def test_where_conjunctive_predicate_still_detected() -> None:
    # An AND at the root drops every unmatched row; the genuine inversion still fires.
    sql = "select * from a left join b on a.k = b.k where b.y > 0 and a.x > 0"
    findings = detect_where_on_outer_joined_nullable(_parse(sql))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.WHERE_ON_OUTER_JOINED_NULLABLE


def test_where_same_side_or_still_detected() -> None:
    # Both disjuncts constrain the same nullable side, so no sibling keeps unmatched rows.
    sql = "select * from a left join b on a.k = b.k where b.y > 0 or b.z > 0"
    findings = detect_where_on_outer_joined_nullable(_parse(sql))
    assert len(findings) >= 1
    assert all(f.kind is FindingKind.WHERE_ON_OUTER_JOINED_NULLABLE for f in findings)


def test_where_or_sibling_on_other_optional_side_still_detected() -> None:
    # Two independent LEFT joins: a row matching neither b nor c has both sides NULL, so
    # `b.y > 0 or c.z > 0` is UNKNOWN and the row drops. A sibling on another optional side
    # is not guaranteed present on the dropped rows, so it does not rescue: still a hazard.
    sql = (
        "select * from a left join b on a.k = b.k left join c on a.k = c.k where b.y > 0 or c.z > 0"
    )
    findings = detect_where_on_outer_joined_nullable(_parse(sql))
    assert len(findings) >= 1
    assert all(f.kind is FindingKind.WHERE_ON_OUTER_JOINED_NULLABLE for f in findings)


def test_coalesce_on_join_key_in_on_clause_detected() -> None:
    # COALESCE in the match condition itself turns non-matches into sentinel
    # matches: the load-bearing position for this hazard.
    sql = """
    select a.id
    from a left join b on coalesce(a.k, 0) = coalesce(b.k, 0)
    """
    findings = detect_coalesce_on_join_key(_parse(sql))
    assert len(findings) >= 1
    assert all(f.kind is FindingKind.COALESCE_ON_JOIN_KEY for f in findings)


def test_coalesce_on_join_key_in_projection_not_detected() -> None:
    # The projection-list coalesce of a join key is the FULL/RIGHT merge idiom
    # (recover the key from whichever side matched), a guard, not a hazard (#139).
    sql = """
    select coalesce(a.k, 0) as k_safe
    from a left join b on a.k = b.k
    """
    assert detect_coalesce_on_join_key(_parse(sql)) == ()


def test_coalesce_full_outer_merge_in_projection_not_detected() -> None:
    # The canonical FULL OUTER union idiom: prefer one feed, fall back to the other.
    sql = """
    select coalesce(a.k, b.k) as k, coalesce(a.v, b.v) as v
    from a full outer join b on a.k = b.k
    """
    assert detect_coalesce_on_join_key(_parse(sql)) == ()


def test_coalesce_on_non_join_column_not_detected() -> None:
    # A COALESCE on a projected column that is not part of any ON clause is silent:
    # the hazard is scoped to the match condition, not arbitrary projection cleanup.
    sql = """
    select coalesce(a.name, '') as name
    from a left join b on a.id = b.id
    """
    assert detect_coalesce_on_join_key(_parse(sql)) == ()


def test_coalesce_on_filter_pushed_into_on_clause_not_detected() -> None:
    # `coalesce(b.flag, true)` is a filter in the ON clause, not a match key, so it must
    # not be mistaken for a masked join key. Only the equality predicate's columns are keys.
    sql = """
    select a.id
    from a left join b on a.id = b.id and coalesce(b.flag, true)
    """
    assert detect_coalesce_on_join_key(_parse(sql)) == ()


def test_unordered_row_number_detected() -> None:
    # The label lands on an arbitrary row, and that row's `payload` is surfaced, so which
    # value pairs with rank 1 is observable: a genuine non-determinism.
    p = _parse("select payload, row_number() over (partition by x) as rn from t")
    findings = detect_unordered_window(p)
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNORDERED_RANKING_WINDOW


def test_ordered_row_number_not_flagged() -> None:
    p = _parse("select row_number() over (order by ts) as rn from t")
    assert detect_unordered_window(p) == ()


# --- row_number() dedup whose PARTITION BY covers the carried columns (#171) ---
# An ORDER-BY-less row_number() is non-deterministic only when the row each rank lands on
# is observable. When every column the ranked scope carries forward is a partition key,
# all rows in a partition are identical on the surfaced columns, so the result bag is the
# same whichever physical row each rank picks. These dedups are silent. The moment a
# non-partition column is surfaced, which row wins is observable again and the finding
# returns.


def test_row_number_qualify_dedup_partition_covers_projection_silent() -> None:
    sql = "select id, a, b from src qualify row_number() over (partition by id, a, b) = 1"
    assert detect_unordered_window(_parse(sql)) == ()


def test_row_number_subquery_dedup_partition_covers_projection_silent() -> None:
    sql = """
    select id, a, b
    from (
      select id, a, b, row_number() over (partition by id, a, b) as rn from src
    ) where rn = 1
    """
    assert detect_unordered_window(_parse(sql)) == ()


def test_row_number_cte_dedup_partition_covers_projection_silent() -> None:
    sql = """
    with d as (
      select id, a, b, row_number() over (partition by id, a, b) as rn from src
    )
    select id, a, b from d where rn = 1
    """
    assert detect_unordered_window(_parse(sql)) == ()


def test_row_number_dedup_surfacing_only_covered_columns_silent() -> None:
    # rn itself is carried forward, but the ranked scope's own output bag is already
    # deterministic (identical rows get the rank labels 1..n as a fixed bag), so a
    # consumer that surfaces rn stays deterministic.
    sql = """
    select id, a, b, rn
    from (
      select id, a, b, row_number() over (partition by id, a, b) as rn from src
    ) where rn = 1
    """
    assert detect_unordered_window(_parse(sql)) == ()


def test_row_number_dedup_projection_expression_over_covered_columns_silent() -> None:
    sql = "select id, lower(b) as lb from src qualify row_number() over (partition by id, b) = 1"
    assert detect_unordered_window(_parse(sql)) == ()


def test_row_number_dedup_carrying_non_partition_column_fires() -> None:
    sql = """
    select id, payload
    from (
      select id, payload, row_number() over (partition by id) as rn from src
    ) where rn = 1
    """
    findings = detect_unordered_window(_parse(sql))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNORDERED_RANKING_WINDOW


def test_row_number_qualify_mixing_non_covered_predicate_fires() -> None:
    # The QUALIFY combines the rank with a predicate on a non-partition column, so whether
    # the surviving row also satisfies `payload > 5` depends on which row got rank 1.
    sql = "select id from src qualify row_number() over (partition by id) = 1 and payload > 5"
    findings = detect_unordered_window(_parse(sql))
    assert len(findings) == 1


def test_row_number_qualify_referencing_window_alias_silent() -> None:
    # QUALIFY names the window by its SELECT alias rather than inlining it. That reference is
    # the rank label, not a carried data column, so the dedup is as silent as the inline form.
    sql = "select id, a, b, row_number() over (partition by id, a, b) as rn from src qualify rn = 1"
    assert detect_unordered_window(_parse(sql)) == ()


def test_row_number_qualify_alias_with_non_covered_predicate_fires() -> None:
    # The alias reference is allowed, but the QUALIFY also pins a non-partition column, so
    # which row survives is observable: the finding stays live.
    sql = (
        "select id, row_number() over (partition by id) as rn from src "
        "qualify rn = 1 and payload > 5"
    )
    findings = detect_unordered_window(_parse(sql))
    assert len(findings) == 1


def test_row_number_dedup_star_projection_fires() -> None:
    # A star can carry any column out of the partition key, so coverage is unprovable.
    sql = "select * from src qualify row_number() over (partition by id) = 1"
    findings = detect_unordered_window(_parse(sql))
    assert len(findings) == 1


def test_unordered_rank_dedup_not_suppressed() -> None:
    # The coverage argument is sound for other ranking functions too, but the dedup idiom
    # this refinement targets is row_number(); rank()/dense_rank() stay flagged.
    sql = "select id, a, b from src qualify rank() over (partition by id, a, b) = 1"
    findings = detect_unordered_window(_parse(sql))
    assert len(findings) == 1


def test_value_window_over_covered_partition_not_suppressed() -> None:
    # first_value reads a value rather than a rank label; the refinement is scoped to
    # row_number(), so a value window stays flagged even when its inputs are covered.
    sql = "select id, a, first_value(a) over (partition by id, a) as fa from src"
    findings = detect_unordered_window(_parse(sql))
    assert len(findings) == 1


def test_row_number_no_partition_stays_flagged() -> None:
    sql = "select id, row_number() over () as rn from t"
    findings = detect_unordered_window(_parse(sql))
    assert len(findings) == 1


def test_row_number_dedup_with_group_by_stays_flagged() -> None:
    # A GROUP BY changes what the ranked scope carries; the dedup coverage argument does
    # not apply, so stay conservative.
    sql = "select id from src group by id qualify row_number() over (partition by id) = 1"
    findings = detect_unordered_window(_parse(sql))
    assert len(findings) == 1


def test_two_unordered_row_numbers_in_one_scope_stay_flagged() -> None:
    # The output-bag argument needs the row_number() to be the only window shaping the
    # scope's output; a second unordered window keeps both findings live.
    sql = """
    select id,
           row_number() over (partition by id) as rn,
           row_number() over (partition by id) as rn2
    from src
    """
    findings = detect_unordered_window(_parse(sql))
    assert len(findings) == 2


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


@pytest.mark.parametrize(
    "sql",
    [
        # sqlglot wraps the ORDER BY under a Limit for the top-n idiom, and can
        # stack a Distinct underneath. The detector must see through both.
        "select array_agg(x order by y limit 1) as arr from t",
        "select array_agg(distinct x order by y limit 1) as arr from t",
        "select string_agg(name, ',' order by ts limit 3) as names from t",
    ],
)
def test_ordered_aggregate_with_inner_limit_not_flagged(sql: str) -> None:
    assert detect_unordered_aggregate(_parse(sql)) == ()


def test_unordered_aggregate_over_ordered_subquery_arg_is_flagged() -> None:
    # The ORDER BY here belongs to the subquery argument, not the aggregate's
    # own ordering, so the aggregate IS unordered. Pins the contract that the
    # detector unwraps the aggregate's clause rather than scanning the subtree.
    p = _parse("select array_agg((select v from u order by w limit 1)) as arr from t")
    findings = detect_unordered_aggregate(p)
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNORDERED_AGGREGATE


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


# --- Inner array-flatten row drop (#63) ---


def _parse_d(sql: str, dialect: str) -> Expr:
    return parse_sql(sql, dialect=dialect)


@pytest.mark.parametrize(
    "sql",
    [
        "select t.id, u.x from t, unnest(t.arr) as u(x)",
        "select t.id, u.x from t cross join unnest(t.arr) as u(x)",
    ],
)
def test_inner_unnest_duckdb_flagged(sql: str) -> None:
    findings = detect_inner_flatten_row_drop(_parse_d(sql, "duckdb"))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.INNER_FLATTEN_ROW_DROP


def test_left_join_unnest_duckdb_not_flagged() -> None:
    sql = "select t.id, u.x from t left join unnest(t.arr) as u(x) on true"
    assert detect_inner_flatten_row_drop(_parse_d(sql, "duckdb")) == ()


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select t.id, x from t, unnest(t.arr) as x", 1),
        ("select t.id, x from t cross join unnest(t.arr) as x", 1),
        ("select t.id, x from t left join unnest(t.arr) as x", 0),
    ],
)
def test_inner_unnest_bigquery(sql: str, expected: int) -> None:
    assert len(detect_inner_flatten_row_drop(_parse_d(sql, "bigquery"))) == expected


def test_snowflake_lateral_flatten_inner_flagged() -> None:
    sql = "select t.id, f.value from t, lateral flatten(input => t.arr) f"
    findings = detect_inner_flatten_row_drop(_parse_d(sql, "snowflake"))
    assert len(findings) == 1


def test_snowflake_lateral_flatten_outer_kwarg_silent() -> None:
    # `FLATTEN(... OUTER => TRUE)` emits a NULL row for an empty/null array, preserving the
    # parent row. The OUTER marker is a kwarg of the flatten call, not the lateral wrapper.
    sql = "select t.id, f.value from t, lateral flatten(input => t.arr, outer => true) f"
    assert detect_inner_flatten_row_drop(_parse_d(sql, "snowflake")) == ()


def test_spark_lateral_view_explode_inner_flagged_outer_silent() -> None:
    inner = "select t.id, x from t lateral view explode(t.arr) tt as x"
    outer = "select t.id, x from t lateral view outer explode(t.arr) tt as x"
    assert len(detect_inner_flatten_row_drop(_parse_d(inner, "spark"))) == 1
    assert detect_inner_flatten_row_drop(_parse_d(outer, "spark")) == ()


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # The outer-ness of explode_outer / posexplode_outer lives in the function itself,
        # not an OUTER keyword, yet they preserve the parent row and must stay silent.
        ("select t.id, x from t lateral view explode_outer(t.arr) tt as x", 0),
        ("select t.id, p, x from t lateral view posexplode_outer(t.arr) tt as p, x", 0),
        ("select t.id, p, x from t lateral view posexplode(t.arr) tt as p, x", 1),
    ],
)
def test_spark_explode_outer_variants(sql: str, expected: int) -> None:
    assert len(detect_inner_flatten_row_drop(_parse_d(sql, "spark"))) == expected


@pytest.mark.parametrize(
    "sql",
    [
        # The wide-to-long pivot idiom: a constructed array of N literal structs is never
        # empty, so the cross join drops no parent row.
        "select t.id, m.metric_name, m.metric_value from t, "
        "unnest(array[struct('clicks' as metric_name, t.clicks as metric_value), "
        "struct('impressions' as metric_name, t.impressions as metric_value)]) as m",
        "select t.id, x from t cross join unnest(array[1, 2, 3]) as x",
    ],
)
def test_inner_unnest_of_nonempty_literal_array_not_flagged(sql: str) -> None:
    assert detect_inner_flatten_row_drop(_parse_d(sql, "bigquery")) == ()


def test_inner_unnest_of_empty_literal_array_still_flagged() -> None:
    # An empty constructor carries no non-emptiness guarantee (degenerate, but pinned).
    sql = "select t.id, x from t cross join unnest(array[]) as x"
    assert len(detect_inner_flatten_row_drop(_parse_d(sql, "bigquery"))) == 1


def test_inner_unnest_of_array_subquery_still_flagged() -> None:
    # ARRAY(SELECT ...) also parses to exp.Array, but the subquery may return zero rows, so
    # the array can be empty and the parent row can still drop.
    sql = "select t.id, x from t cross join unnest(array(select v from u where u.id = t.id)) as x"
    assert len(detect_inner_flatten_row_drop(_parse_d(sql, "bigquery"))) == 1


def test_inner_unnest_of_scalar_subquery_array_literal_not_flagged() -> None:
    # The real-world pivot idiom: a bracket array of N parenthesised `(SELECT AS STRUCT ...)`
    # scalar subqueries. Each contributes exactly one element, so the array is non-empty.
    sql = (
        "select t.id, m.metric_name, m.metric_value from t cross join unnest([ "
        "(select as struct 'clicks' as metric_name, t.clicks as metric_value), "
        "(select as struct 'impressions' as metric_name, t.impressions as metric_value)]) as m"
    )
    assert detect_inner_flatten_row_drop(_parse_d(sql, "bigquery")) == ()


def test_inner_unnest_of_column_still_flagged_without_a_predicate() -> None:
    # A column array's non-emptiness needs the lineage-grounded predicate; the bare
    # structural detector treats a column as opaque and keeps firing.
    sql = "select t.id, x from t cross join unnest(t.arr) as x"
    assert len(detect_inner_flatten_row_drop(_parse_d(sql, "bigquery"))) == 1


def test_inner_unnest_of_column_cleared_by_predicate() -> None:
    sql = "select s.id, x from stg s cross join unnest(s.tags) as x"
    tree = _parse_d(sql, "bigquery")
    assert len(detect_inner_flatten_row_drop(tree)) == 1
    cleared = detect_inner_flatten_row_drop(tree, column_is_nonempty=lambda c: c.name == "tags")
    assert cleared == ()


def test_predicate_only_clears_the_columns_it_vouches_for() -> None:
    sql = "select s.id, x from stg s cross join unnest(s.other) as x"
    tree = _parse_d(sql, "bigquery")
    findings = detect_inner_flatten_row_drop(tree, column_is_nonempty=lambda c: c.name == "tags")
    assert len(findings) == 1


def test_predicate_does_not_clear_a_column_from_the_nullable_join_side() -> None:
    # A column vouched non-empty where it is produced can still arrive NULL through the
    # nullable side of an outer join, where UNNEST(NULL) drops the row; the predicate must
    # not clear it. The same column on a preserved side (cross join) clears as usual.
    nullable = (
        "select d.id, x from drv d left join stg s on s.id = d.id cross join unnest(s.tags) as x"
    )
    tree = _parse_d(nullable, "bigquery")
    assert (
        len(detect_inner_flatten_row_drop(tree, column_is_nonempty=lambda c: c.name == "tags")) == 1
    )

    preserved = "select s.id, x from stg s cross join unnest(s.tags) as x"
    cleared = _parse_d(preserved, "bigquery")
    assert (
        detect_inner_flatten_row_drop(cleared, column_is_nonempty=lambda c: c.name == "tags") == ()
    )


def test_plain_cross_join_of_tables_not_flagged() -> None:
    # A cartesian product of two relations is not an array flatten; not our hazard.
    sql = "select * from a cross join b"
    assert detect_inner_flatten_row_drop(_parse_d(sql, "duckdb")) == ()


def test_lateral_subquery_not_flatten_not_flagged() -> None:
    # A LATERAL over a subquery (not an array flatten) does not drop rows on empty arrays.
    sql = "select t.id, s.v from t, lateral (select max(x) as v from u where u.id = t.id) s"
    assert detect_inner_flatten_row_drop(_parse_d(sql, "postgres")) == ()


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
    select b.k,
           row_number() over (partition by b.k) as rn,
           array_agg(amount) as amounts
    from a left join b on coalesce(a.k, 0) = coalesce(b.k, 0)
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


@given(cols=st.lists(_IDENT, min_size=1, max_size=4, unique=True), extra=_IDENT)
@settings(max_examples=50, deadline=None)
def test_qualify_dedup_silent_iff_projection_covered(cols: list[str], extra: str) -> None:
    """A row_number()=1 dedup is silent exactly when the projection stays within the
    partition key; surfacing any column outside it brings the finding back."""
    pcsv = ", ".join(cols)
    covered = f"select {pcsv} from src qualify row_number() over (partition by {pcsv}) = 1"
    assert detect_unordered_window(_parse(covered)) == ()
    if extra in cols:
        return
    uncovered = (
        f"select {pcsv}, {extra} from src qualify row_number() over (partition by {pcsv}) = 1"
    )
    findings = detect_unordered_window(_parse(uncovered))
    assert len(findings) == 1


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
