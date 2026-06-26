"""Tests for the fact-grounded detectors (window order-keys, join fanout).

The detectors consume substrate-derived keys: ``model_keys`` maps a relation name
to its candidate keys (what cross-model propagation produced), and a per-tree
scope index (computed on demand here) supplies CTE and inline-subquery keys.
"""

from __future__ import annotations

from sqlglot import Expr

from dblect.adapters import profile_for_adapter
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType
from dblect.sql import FindingKind, parse_sql
from dblect.uniqueness.detector import (
    detect_join_fanout,
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
    window_keys, _fanout = make_fact_grounded_detectors(
        manifest, _DUCKDB, parsed={model.unique_id: tree}
    )
    findings = window_keys(tree)
    # `orders_v2` is unique on (id); the window's (customer_id, ts) key is not
    # covered, so the non-deterministic ranking is flagged.
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS
