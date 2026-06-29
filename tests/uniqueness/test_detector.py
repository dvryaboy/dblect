"""Tests for the fact-grounded detectors (window order-keys, join fanout).

The detectors consume substrate-derived keys: ``model_keys`` maps a relation name
to its candidate keys (what cross-model propagation produced), and a per-tree
scope index (computed on demand here) supplies CTE and inline-subquery keys.
"""

from __future__ import annotations

import pytest
from sqlglot import Expr

from dblect.adapters import profile_for_adapter
from dblect.manifest import DbtTestMetadata, Manifest, ModelConfig, Node, ResourceType
from dblect.sql import Finding, FindingKind, parse_sql
from dblect.uniqueness.detector import (
    detect_join_fanout,
    detect_limit_without_deterministic_order,
    detect_non_unique_aggregate_order_keys,
    detect_non_unique_window_order_keys,
    make_fact_grounded_detectors,
)

_DUCKDB = profile_for_adapter("duckdb")

_Keys = dict[str, frozenset[frozenset[str]]]


def _model_keys(**name_to_keys: tuple[tuple[str, ...], ...]) -> _Keys:
    """Per-relation candidate keys by name: ``_model_keys(src=[("id",)])``."""
    return {
        name: frozenset(frozenset(cols) for cols in keys) for name, keys in name_to_keys.items()
    }


def _parse(sql: str) -> Expr:
    return parse_sql(sql, dialect="duckdb")


def test_window_order_keys_not_unique_is_flagged() -> None:
    # `src` is unique on (id), but the window orders by ts within partition
    # customer_id. Combined key (customer_id, ts) isn't covered by (id).
    parsed = _parse(
        "select row_number() over (partition by customer_id order by ts) as rn from src"
    )
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(src=(("id",),)))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS


def test_window_keys_covered_by_declared_unique_key_is_silent() -> None:
    parsed = _parse("select row_number() over (partition by customer_id order by ts) from src")
    findings = detect_non_unique_window_order_keys(
        parsed, model_keys=_model_keys(src=(("customer_id", "ts"),))
    )
    assert findings == ()


def test_superkey_covered_by_subset_fact_is_silent() -> None:
    # `id` alone is unique on the source; (id, ts) is a superkey and still unique.
    parsed = _parse("select row_number() over (partition by id order by ts) from src")
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(src=(("id",),)))
    assert findings == ()


def test_no_order_by_window_is_not_flagged() -> None:
    parsed = _parse("select row_number() over (partition by customer_id) from src")
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(src=(("id",),)))
    assert findings == ()


def test_no_keys_for_source_stays_silent() -> None:
    parsed = _parse("select row_number() over (partition by customer_id order by ts) from src")
    findings = detect_non_unique_window_order_keys(parsed, model_keys={})
    assert findings == ()


def test_unresolved_source_name_stays_silent() -> None:
    parsed = _parse(
        "select row_number() over (partition by customer_id order by ts) from unknown_table"
    )
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(src=(("id",),)))
    assert findings == ()


def test_join_at_top_level_is_out_of_scope() -> None:
    parsed = _parse(
        "select row_number() over (partition by a.id order by a.ts) "
        "from src a join other b on a.k = b.k"
    )
    findings = detect_non_unique_window_order_keys(
        parsed, model_keys=_model_keys(src=(("id",),), other=(("id",),))
    )
    assert findings == ()


def test_order_by_expression_is_skipped() -> None:
    parsed = _parse(
        "select row_number() over (partition by customer_id order by date_trunc('day', ts)) from src"
    )
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(src=(("id",),)))
    assert findings == ()


def test_window_against_cte_inherits_model_keys_via_propagation() -> None:
    # The CTE `src` pass-throughs `raw`, so its keys propagate. The window's
    # (customer_id, ts) tuple isn't covered by `raw`'s key (id), so flag.
    parsed = _parse(
        "with src as (select * from raw) "
        "select row_number() over (partition by customer_id order by ts) from src"
    )
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(raw=(("id",),)))
    assert len(findings) == 1


def test_window_against_cte_covered_via_propagation_is_silent() -> None:
    parsed = _parse(
        "with src as (select * from raw) "
        "select row_number() over (partition by id order by ts) from src"
    )
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(raw=(("id",),)))
    assert findings == ()


# --- top-level LIMIT without a deterministic ORDER BY ------------------------
#
# A persisted model whose top scope has `LIMIT n` freezes an arbitrary slice of rows unless the
# rows are totally ordered by a known uniqueness key, or the scope yields at most one row. Each
# case below is the minimal repro of one branch of that verdict.


def _limit(sql: str, keys: _Keys, *, materialized: bool = True) -> tuple[Finding, ...]:
    return detect_limit_without_deterministic_order(
        _parse(sql), model_keys=keys, is_materialized=materialized
    )


_ORDERS_ON_ID = _model_keys(orders=(("id",),))
_UNION_ON_ID = _model_keys(orders=(("id",),), returns=(("id",),))

# (branch id, sql, model keys, is_materialized, expected-to-fire).
_LIMIT_CASES = [
    # No ORDER BY fires without grounding (no source key needed to know the slice is unpinned).
    ("no_order_no_keys", "select id from orders limit 10", _model_keys(), True, True),
    (
        "order_not_covering",
        "select id from orders order by total limit 10",
        _ORDERS_ON_ID,
        True,
        True,
    ),
    ("order_covers_key", "select id from orders order by id limit 10", _ORDERS_ON_ID, True, False),
    (
        "order_superkey",
        "select id from orders order by id, total limit 10",
        _ORDERS_ON_ID,
        True,
        False,
    ),
    ("no_limit", "select id from orders order by id", _ORDERS_ON_ID, True, False),
    ("not_persisted", "select id from orders limit 10", _ORDERS_ON_ID, False, False),
    (
        "order_unknown_keys",
        "select id from orders order by total limit 10",
        _model_keys(),
        True,
        False,
    ),
    (
        "order_expression",
        "select id from orders order by date_trunc('day', ts) limit 10",
        _ORDERS_ON_ID,
        True,
        False,
    ),
    (
        "limit_in_subquery",
        "select id from (select id from orders limit 5) s",
        _ORDERS_ON_ID,
        True,
        False,
    ),
    (
        "union_top_scope",
        "(select id from orders) union all (select id from returns) limit 5",
        _UNION_ON_ID,
        True,
        False,
    ),
    ("ungrouped_count", "select count(*) from orders limit 10", _ORDERS_ON_ID, True, False),
    (
        "grouped_aggregate",
        "select customer_id, count(*) from orders group by customer_id limit 10",
        _ORDERS_ON_ID,
        True,
        True,
    ),
    (
        "windowed_aggregate",
        "select count(*) over () from orders limit 10",
        _ORDERS_ON_ID,
        True,
        True,
    ),
    (
        "order_alias_of_key",
        "select id as oid from orders order by oid limit 10",
        _ORDERS_ON_ID,
        True,
        False,
    ),
    (
        "order_alias_renames_non_key",
        "select other as id from orders order by id limit 10",
        _ORDERS_ON_ID,
        True,
        True,
    ),
]


@pytest.mark.parametrize(
    ("sql", "keys", "materialized", "fires"),
    [case[1:] for case in _LIMIT_CASES],
    ids=[case[0] for case in _LIMIT_CASES],
)
def test_limit_verdict(sql: str, keys: _Keys, materialized: bool, fires: bool) -> None:
    findings = _limit(sql, keys, materialized=materialized)
    if fires:
        assert len(findings) == 1
        assert findings[0].kind is FindingKind.LIMIT_WITHOUT_DETERMINISTIC_ORDER
    else:
        assert findings == ()


# --- top-n aggregate (ARRAY_AGG/STRING_AGG ... ORDER BY k LIMIT n) -----------
#
# An ordered aggregate that keeps only some elements (`ARRAY_AGG(x ORDER BY k LIMIT n)`, the
# top-n idiom) is deterministic in *which* elements survive only when the order key is unique
# within the group. With ties at the cutoff the LIMIT keeps an arbitrary winner, so the result
# drifts run to run. This is the aggregate analog of the window order-key check: the GROUP BY
# columns play the partition's role. Each case is the minimal repro of one branch of the verdict.


def _agg_order(sql: str, keys: _Keys) -> tuple[Finding, ...]:
    return detect_non_unique_aggregate_order_keys(_parse(sql), model_keys=keys)


_SRC_ON_ID = _model_keys(src=(("id",),))

# (branch id, sql, model keys, expected-to-fire).
_AGG_ORDER_CASES = [
    # Whole-relation top-1 by a non-unique key: which row survives is arbitrary.
    (
        "whole_relation_non_unique",
        "select array_agg(x order by ts limit 1) from src",
        _SRC_ON_ID,
        True,
    ),
    # Grouped top-1: within each group `ts` is not unique, so the combined (g, ts) key isn't
    # covered by (id).
    (
        "grouped_non_unique",
        "select g, array_agg(x order by ts limit 1) from src group by g",
        _SRC_ON_ID,
        True,
    ),
    # Order key is the source key: the top-n cut is total, silent.
    ("order_covers_key", "select array_agg(x order by id limit 1) from src", _SRC_ON_ID, False),
    # Group + order together cover a composite key, silent.
    (
        "group_plus_order_covers_key",
        "select g, array_agg(x order by seq limit 1) from src group by g",
        _model_keys(src=(("g", "seq"),)),
        False,
    ),
    # Superkey of the unique key still totally orders, silent.
    ("order_superkey", "select array_agg(x order by id, ts limit 1) from src", _SRC_ON_ID, False),
    # No inner LIMIT: every element survives, so membership is deterministic (only the internal
    # tie order is unstable, which this check leaves to the unordered-aggregate detector). Silent.
    ("no_limit", "select array_agg(x order by ts) from src", _SRC_ON_ID, False),
    # No ORDER BY at all is the unordered-aggregate detector's job, not this one. Silent here.
    ("no_order", "select array_agg(x limit 1) from src", _SRC_ON_ID, False),
    # No known key on the source: firewall posture, no positive fact to fire on.
    ("no_keys", "select array_agg(x order by ts limit 1) from src", _model_keys(), False),
    # Non-bare order key needs an equivalence we don't model, silent.
    (
        "order_expression",
        "select array_agg(x order by date_trunc('day', ts) limit 1) from src",
        _SRC_ON_ID,
        False,
    ),
    # Non-bare grouping key, likewise silent.
    (
        "group_expression",
        "select date_trunc('day', ts) d, array_agg(x order by k limit 1) from src "
        "group by date_trunc('day', ts)",
        _SRC_ON_ID,
        False,
    ),
    # Multi-source scope needs column-level lineage, silent.
    (
        "join_out_of_scope",
        "select array_agg(a.x order by a.ts limit 1) from src a join other b on a.k = b.k",
        _model_keys(src=(("id",),), other=(("id",),)),
        False,
    ),
    # Unresolved source name, silent.
    ("unknown_source", "select array_agg(x order by ts limit 1) from unknown", _SRC_ON_ID, False),
    # STRING_AGG (sqlglot's GroupConcat) is order-sensitive too, fires.
    ("string_agg", "select string_agg(x, ',' order by ts limit 1) from src", _SRC_ON_ID, True),
    # The DISTINCT modifier sqlglot wraps around the ORDER BY is seen through, fires.
    (
        "distinct_modifier",
        "select array_agg(distinct x order by ts limit 1) from src",
        _SRC_ON_ID,
        True,
    ),
]


@pytest.mark.parametrize(
    ("sql", "keys", "fires"),
    [case[1:] for case in _AGG_ORDER_CASES],
    ids=[case[0] for case in _AGG_ORDER_CASES],
)
def test_aggregate_order_verdict(sql: str, keys: _Keys, fires: bool) -> None:
    findings = _agg_order(sql, keys)
    if fires:
        assert len(findings) == 1
        assert findings[0].kind is FindingKind.NON_UNIQUE_AGGREGATE_ORDER_KEYS
    else:
        assert findings == ()


def test_aggregate_order_against_cte_inherits_keys_via_propagation() -> None:
    # The CTE `src` pass-throughs `raw`; its key (id) propagates. The top-1 by `ts` isn't
    # covered, so flag — a CTE source is checkable like a ref'd model.
    parsed = _parse(
        "with src as (select * from raw) select array_agg(x order by ts limit 1) from src"
    )
    findings = detect_non_unique_aggregate_order_keys(
        parsed, model_keys=_model_keys(raw=(("id",),))
    )
    assert len(findings) == 1


def test_aggregate_order_finding_carries_line_number() -> None:
    sql = "select\n  array_agg(x order by ts limit 1) as latest\nfrom src\n"
    findings = detect_non_unique_aggregate_order_keys(_parse(sql), model_keys=_SRC_ON_ID)
    assert len(findings) == 1
    assert findings[0].line_start == 2


def test_window_against_inline_subquery_inherits_keys() -> None:
    # The inline subquery `(select * from raw)` pass-throughs `raw`, so its keys
    # propagate. The window's (customer_id, ts) tuple isn't covered by raw's key
    # (id), so flag — an inline subquery source is checkable, like a CTE.
    parsed = _parse(
        "select row_number() over (partition by customer_id order by ts) "
        "from (select * from raw) sub"
    )
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(raw=(("id",),)))
    assert len(findings) == 1


def test_window_against_inline_subquery_covered_is_silent() -> None:
    # Same pass-through, but the window partitions by id; the inherited key covers
    # the (id, ts) superkey, so no finding.
    parsed = _parse(
        "select row_number() over (partition by id order by ts) from (select * from raw) sub"
    )
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(raw=(("id",),)))
    assert findings == ()


def test_multiple_windows_each_evaluated_independently() -> None:
    parsed = _parse(
        "select "
        "  row_number() over (partition by customer_id order by ts) as rn, "
        "  rank() over (partition by id order by ts) as rk "
        "from src"
    )
    findings = detect_non_unique_window_order_keys(parsed, model_keys=_model_keys(src=(("id",),)))
    # First window (customer_id, ts) not covered → flagged; second (id, ts) is a
    # superkey of `id` → silent.
    assert len(findings) == 1
    assert "customer_id" in findings[0].sql_snippet


def test_finding_carries_line_number() -> None:
    sql = "select\n  row_number() over (partition by customer_id order by ts) as rn\nfrom src\n"
    findings = detect_non_unique_window_order_keys(
        _parse(sql), model_keys=_model_keys(src=(("id",),))
    )
    assert len(findings) == 1
    assert findings[0].line_start == 2


# --- join fanout ---


def test_fanout_flagged_when_keys_dont_cover_join_key() -> None:
    parsed = _parse("select * from facts f left join dim d on f.segment = d.segment")
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.JOIN_FANOUT
    assert "segment" in findings[0].message


def test_fanout_silent_when_join_key_is_a_declared_unique_key() -> None:
    parsed = _parse("select * from facts f left join dim d on f.id = d.id")
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_silent_when_join_key_is_a_superkey_of_declared_key() -> None:
    parsed = _parse(
        "select * from facts f left join dim d on f.id = d.id and f.segment = d.segment"
    )
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_composite_key_silent_when_join_covers_all_columns() -> None:
    parsed = _parse("select * from facts f join dim d on f.a = d.a and f.b = d.b")
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("a", "b"),)))
    assert findings == ()


def test_fanout_composite_key_flagged_when_join_covers_only_one_column() -> None:
    parsed = _parse("select * from facts f join dim d on f.a = d.a")
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("a", "b"),)))
    assert len(findings) == 1


def test_fanout_silent_when_source_has_no_keys() -> None:
    parsed = _parse("select * from facts f left join dim d on f.segment = d.segment")
    findings = detect_join_fanout(parsed, model_keys={})
    assert findings == ()


def test_fanout_silent_when_join_target_is_unknown_model() -> None:
    parsed = _parse("select * from facts f left join unknown u on f.id = u.id")
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_silent_on_cross_join() -> None:
    parsed = _parse("select * from facts f cross join dim d")
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_silent_when_join_target_shadowed_by_cte() -> None:
    # A local CTE named `dim` shadows the model: resolution lands on the CTE,
    # whose body has no known keys, so the detector stays silent.
    parsed = _parse(
        "with dim as (select segment from raw) "
        "select * from facts f left join dim d on f.segment = d.segment"
    )
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_silent_when_predicate_is_disjunctive() -> None:
    parsed = _parse("select * from facts f left join dim d on f.id = d.id or f.alt = d.alt")
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_silent_when_predicate_has_function_call() -> None:
    parsed = _parse("select * from facts f left join dim d on lower(f.id) = lower(d.id)")
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_flagged_inside_cte_body() -> None:
    parsed = _parse(
        "with widened as ("
        "  select * from facts f left join dim d on f.segment = d.segment"
        ") "
        "select * from widened"
    )
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert len(findings) == 1


def test_fanout_silent_when_cte_inherits_uniqueness_via_propagation() -> None:
    # The CTE `dim_local` pass-throughs `dim`, so its propagated key is `id`; the
    # join binds on `id`, so it can't fan out.
    parsed = _parse(
        "with dim_local as (select * from dim) "
        "select * from facts f join dim_local d on f.id = d.id"
    )
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_flagged_when_join_to_propagated_cte_misses_inherited_key() -> None:
    # Same propagated key, but the join binds on `segment` rather than the
    # inherited `id` key, so it can fan out.
    parsed = _parse(
        "with dim_local as (select * from dim) "
        "select * from facts f join dim_local d on f.segment = d.segment"
    )
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.JOIN_FANOUT


def test_fanout_finding_carries_join_line() -> None:
    sql = "select *\nfrom facts f\nleft join dim d on f.segment = d.segment\n"
    findings = detect_join_fanout(_parse(sql), model_keys=_model_keys(dim=(("id",),)))
    assert len(findings) == 1
    assert findings[0].line_start == 3


# --- fan-out collapsed in-query before a sensitive consumer (#170) ---


def test_fanout_silent_when_collapsed_by_group_with_insensitive_aggregate() -> None:
    # The fan-out is real, but `group by` collapses it and the only consumer of the
    # joined rows is `max`, which is duplicate-insensitive. No output hazard.
    parsed = _parse(
        "select f.id, f.entity, max(d.seen_at) as last_seen "
        "from facts f join dim d on f.segment = d.segment "
        "group by f.id, f.entity"
    )
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_flagged_when_group_feeds_a_sensitive_aggregate() -> None:
    # `sum` folds the duplicated rows, so the grouping does not rescue it: still a hazard.
    parsed = _parse(
        "select f.id, sum(f.amount) as total "
        "from facts f join dim d on f.segment = d.segment "
        "group by f.id"
    )
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.JOIN_FANOUT


def test_fanout_silent_when_collapsed_group_uses_distinct_aggregate() -> None:
    # count(distinct ...) deduplicates, so the fan-out cannot change it.
    parsed = _parse(
        "select f.id, count(distinct d.kind) as kinds "
        "from facts f join dim d on f.segment = d.segment "
        "group by f.id"
    )
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert findings == ()


def test_fanout_flagged_without_grouping_even_with_only_insensitive_aggregate() -> None:
    # No GROUP BY: the multiplied rows flow straight to the output, so a windowed or
    # ungrouped read is not collapsed. Raw passthrough keeps the finding firing.
    parsed = _parse("select f.id, d.seen_at from facts f join dim d on f.segment = d.segment")
    findings = detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))
    assert len(findings) == 1


def test_fanout_unknown_udf_aggregate_keeps_firing_unless_declared_idempotent() -> None:
    parsed = _parse(
        "select f.id, geo_mean(d.v) as gm "
        "from facts f join dim d on f.segment = d.segment "
        "group by f.id"
    )
    # An unrecognized UDF aggregate is duplicate-sensitive by default, so the fan-out fires.
    assert len(detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),)))) == 1
    # The adapter naming it duplicate-safe clears the collapse.
    cleared = detect_join_fanout(
        parsed,
        model_keys=_model_keys(dim=(("id",),)),
        duplicate_safe_builtins=frozenset({"geo_mean"}),
    )
    assert cleared == ()


def test_fanout_silent_when_scalar_udf_reads_only_grouping_keys() -> None:
    # `fmt_region(f.region)` is a scalar function over a grouping key, constant within a
    # group, so it cannot fold the multiplied rows. The only real aggregate is `max`
    # (duplicate-safe), so the grouping collapses the fan-out: no output hazard.
    parsed = _parse(
        "select f.id, fmt_region(f.region) as region, max(d.x) as mx "
        "from facts f join dim d on f.segment = d.segment "
        "group by f.id, f.region"
    )
    assert detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),))) == ()


def test_fanout_collapse_ignores_aggregate_in_nested_subquery() -> None:
    # The `sum(o.amt)` aggregate folds rows of `other` in a correlated subquery, a separate
    # scope; it is not a consumer of the multiplied join rows. The outer grouping with only
    # `max` collapses the fan-out.
    parsed = _parse(
        "select f.id, max(d.x) as mx, "
        "(select sum(o.amt) from other o where o.id = f.id) as s "
        "from facts f join dim d on f.segment = d.segment "
        "group by f.id"
    )
    assert detect_join_fanout(parsed, model_keys=_model_keys(dim=(("id",),))) == ()


# --- end-to-end key resolution through make_fact_grounded_detectors ----------
#
# The direct-call tests above hand the detector a ``model_keys`` map keyed the way
# the production indexer produces it. These exercise that indexer
# (``_model_keys_by_name``) so the name a relation is looked up by matches the
# name it appears under in compiled SQL.


def _source_with_identifier(uid: str, *, name: str, identifier: str) -> Node:
    return Node(
        unique_id=uid,
        name=name,
        resource_type=ResourceType.SOURCE,
        fqn=(uid,),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        identifier=identifier,
    )


def _model(uid: str, sql: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
    )


def _unique_test(uid: str, *, column: str, target: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.OTHER,
        fqn=(uid,),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}),
        attached_node=target,
    )


def test_source_keys_resolve_by_compiled_identifier_not_name() -> None:
    """A source whose ``identifier`` diverges from its ``name`` (a common
    ``schema.yml`` setting) appears in compiled SQL under the identifier. The
    detectors must look its keys up by that identifier, matching the relation-graph
    builder. Keyed by ``name`` instead, the declared key would be invisible and the
    hazard would go unflagged."""
    src = _source_with_identifier("source.shop.raw.orders", name="orders", identifier="orders_v2")
    test = _unique_test("test.shop.u", column="id", target=src.unique_id)
    # The compiled SQL references the source by its identifier, as dbt emits it.
    sql = "select row_number() over (partition by customer_id order by ts) as rn from orders_v2"
    model = _model("model.shop.ranked", sql)
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in (src, test, model)},
    )
    tree = _parse(sql)
    window_keys, _fanout, _limit, _agg = make_fact_grounded_detectors(
        manifest, _DUCKDB, parsed={model.unique_id: tree}
    )
    findings = window_keys(tree)
    # `orders_v2` is unique on (id); the window's (customer_id, ts) key is not
    # covered, so the non-deterministic ranking is flagged.
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS


def test_aggregate_order_resolves_keys_through_factory_boundary() -> None:
    """The top-n aggregate detector reaches production through ``make_fact_grounded_detectors``
    and grounds on keys the indexer produces, the same boundary the window check uses."""
    src = _source_with_identifier("source.shop.raw.orders", name="orders", identifier="orders_v2")
    test = _unique_test("test.shop.u", column="id", target=src.unique_id)
    sql = "select array_agg(line order by created_at limit 1) as latest from orders_v2"
    model = _model("model.shop.latest_line", sql)
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in (src, test, model)},
    )
    tree = _parse(sql)
    _window, _fanout, _limit, agg_order = make_fact_grounded_detectors(
        manifest, _DUCKDB, parsed={model.unique_id: tree}
    )
    findings = agg_order(tree)
    # `orders_v2` is unique on (id); the top-1 by `created_at` is not covered, so the
    # non-deterministic winner is flagged.
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NON_UNIQUE_AGGREGATE_ORDER_KEYS


def _materialized_model(uid: str, sql: str, *, materialized: str | None) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
        config=ModelConfig(materialized=materialized),
    )


@pytest.mark.parametrize(
    ("materialized", "fires"),
    [
        # Persisted materializations store the LIMIT's arbitrary slice, so they fire. A snapshot
        # persists an SCD-2 table, hence persisted alongside the obvious table family.
        ("table", True),
        ("incremental", True),
        ("materialized_view", True),
        ("snapshot", True),
        # A view recomputes the query per read and an ephemeral model is inlined into each
        # consumer, so neither stores a slice: the LIMIT is the consumer's question, silent.
        ("view", False),
        ("ephemeral", False),
        # An adapter-specific or absent materialization is not positively persisted, so the
        # firewall posture stays silent rather than guess.
        ("some_adapter_thing", False),
        (None, False),
    ],
)
def test_limit_detector_fires_only_for_persisted_materialization(
    materialized: str | None, fires: bool
) -> None:
    """The factory reads each model's resolved materialization through the public boundary and
    fires only on a positively persisted one. This pins both the persisted/not classification
    and the per-tree materialization plumbing the detector relies on (it learns a tree's
    materialization through the factory, keyed by ``id(tree)``)."""
    sql = "select id from orders limit 10"
    model = _materialized_model("model.shop.m", sql, materialized=materialized)
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={model.unique_id: model},
    )
    tree = _parse(sql)
    _window, _fanout, limit_order, _agg = make_fact_grounded_detectors(
        manifest, _DUCKDB, parsed={model.unique_id: tree}
    )
    findings = limit_order(tree)
    if fires:
        assert len(findings) == 1
        assert findings[0].kind is FindingKind.LIMIT_WITHOUT_DETERMINISTIC_ORDER
    else:
        assert findings == ()
