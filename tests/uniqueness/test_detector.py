"""Tests for the fact-grounded detectors (window order-keys, join fanout)."""

from __future__ import annotations

from collections.abc import Mapping

from sqlglot import Expr

from dblect.sql import FindingKind, parse_sql
from dblect.uniqueness import UniquenessFact, UniquenessSource
from dblect.uniqueness.detector import (
    detect_join_fanout,
    detect_non_unique_window_order_keys,
)


def _facts(model_uid: str, *keys: tuple[str, ...]) -> Mapping[str, tuple[UniquenessFact, ...]]:
    return {
        model_uid: tuple(
            UniquenessFact(
                model_unique_id=model_uid,
                columns=frozenset(cols),
                source=UniquenessSource.DBT_UNIQUE_TEST,
                detail=None,
            )
            for cols in keys
        ),
    }


def _parse(sql: str) -> Expr:
    return parse_sql(sql, dialect="duckdb")


def test_window_order_keys_not_unique_is_flagged() -> None:
    # `src` is unique on (id), but the window orders by ts within partition customer_id.
    # Combined key (customer_id, ts) isn't covered by the known key (id).
    parsed = _parse(
        "select row_number() over (partition by customer_id order by ts) as rn from src"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS


def test_window_keys_covered_by_declared_unique_key_is_silent() -> None:
    # Source is unique on (customer_id, ts); the window's (customer_id, ts) key is
    # therefore guaranteed unique. No finding.
    parsed = _parse("select row_number() over (partition by customer_id order by ts) from src")
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("customer_id", "ts")),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert findings == ()


def test_superkey_covered_by_subset_fact_is_silent() -> None:
    # `id` alone is unique on the source; (id, ts) is a superkey and still unique.
    parsed = _parse("select row_number() over (partition by id order by ts) from src")
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert findings == ()


def test_no_order_by_window_is_not_flagged() -> None:
    # detect_unordered_window covers this case; we don't double-flag.
    parsed = _parse("select row_number() over (partition by customer_id) from src")
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert findings == ()


def test_no_facts_for_source_stays_silent() -> None:
    # We don't know if the source has unique keys, so we can't claim a hazard.
    parsed = _parse("select row_number() over (partition by customer_id order by ts) from src")
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts={},
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert findings == ()


def test_unresolved_source_name_stays_silent() -> None:
    # The FROM table doesn't map to any known model, so we don't reason about it.
    parsed = _parse(
        "select row_number() over (partition by customer_id order by ts) from unknown_table"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert findings == ()


def test_join_at_top_level_is_out_of_scope() -> None:
    # Multi-source joins need column-level lineage; we conservatively skip.
    parsed = _parse(
        "select row_number() over (partition by a.id order by a.ts) "
        "from src a join other b on a.k = b.k"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src", "other": "model.pkg.other"},
    )
    assert findings == ()


def test_order_by_expression_is_skipped() -> None:
    # `order by date_trunc(...)` isn't a bare column; we don't reason about it.
    parsed = _parse(
        "select row_number() over (partition by customer_id order by date_trunc('day', ts)) from src"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert findings == ()


def test_window_against_cte_inherits_model_keys_via_propagation() -> None:
    # The CTE `src` pass-throughs `raw` so its keys propagate. The window's
    # (customer_id, ts) tuple isn't covered by `raw`'s key (id), so flag.
    parsed = _parse(
        "with src as (select * from raw) "
        "select row_number() over (partition by customer_id order by ts) from src"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.raw", ("id",)),
        model_name_to_uid={"raw": "model.pkg.raw"},
    )
    assert len(findings) == 1


def test_window_against_cte_covered_via_propagation_is_silent() -> None:
    # The CTE pass-throughs `raw.id`, and the window partitions by id. The
    # propagated key covers the window key, so no finding.
    parsed = _parse(
        "with src as (select * from raw) "
        "select row_number() over (partition by id order by ts) from src"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.raw", ("id",)),
        model_name_to_uid={"raw": "model.pkg.raw"},
    )
    assert findings == ()


def test_multiple_windows_each_evaluated_independently() -> None:
    parsed = _parse(
        "select "
        "  row_number() over (partition by customer_id order by ts) as rn, "
        "  rank() over (partition by id order by ts) as rk "
        "from src"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    # First window: (customer_id, ts), not covered. Flagged.
    # Second window: (id, ts), superkey of `id`. Silent.
    assert len(findings) == 1
    assert "customer_id" in findings[0].sql_snippet


def test_finding_carries_line_number() -> None:
    sql = "select\n  row_number() over (partition by customer_id order by ts) as rn\nfrom src\n"
    findings = detect_non_unique_window_order_keys(
        _parse(sql),
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert len(findings) == 1
    # The window lives on line 2.
    assert findings[0].line_start == 2


# --- join fanout ---


def test_fanout_flagged_when_facts_dont_cover_join_key() -> None:
    # `dim` has a fact on (id), but we join on (segment), which isn't unique.
    parsed = _parse("select * from facts f left join dim d on f.segment = d.segment")
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.JOIN_FANOUT
    assert "segment" in findings[0].message


def test_fanout_silent_when_join_key_is_a_declared_unique_key() -> None:
    parsed = _parse("select * from facts f left join dim d on f.id = d.id")
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_silent_when_join_key_is_a_superkey_of_declared_key() -> None:
    # Source unique on (id); join uses both id AND segment — still safe.
    parsed = _parse(
        "select * from facts f left join dim d on f.id = d.id and f.segment = d.segment"
    )
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_composite_key_silent_when_join_covers_all_columns() -> None:
    parsed = _parse("select * from facts f join dim d on f.a = d.a and f.b = d.b")
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("a", "b")),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_composite_key_flagged_when_join_covers_only_one_column() -> None:
    parsed = _parse("select * from facts f join dim d on f.a = d.a")
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("a", "b")),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert len(findings) == 1


def test_fanout_silent_when_source_has_no_facts() -> None:
    # Opportunistic: with no facts on the joined-in model, we can't tell.
    parsed = _parse("select * from facts f left join dim d on f.segment = d.segment")
    findings = detect_join_fanout(
        parsed,
        facts={},
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_silent_when_join_target_is_unknown_model() -> None:
    parsed = _parse("select * from facts f left join unknown u on f.id = u.id")
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_silent_on_cross_join() -> None:
    # CROSS is an explicit cartesian; that's not what this detector is for.
    parsed = _parse("select * from facts f cross join dim d")
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_silent_when_join_target_shadowed_by_cte() -> None:
    # A local CTE named `dim` shadows the dbt model: resolution lands on the
    # CTE, and the CTE body has no known keys (no upstream fact on `raw`,
    # no DISTINCT/GROUP BY), so the detector stays silent rather than
    # claiming a hazard.
    parsed = _parse(
        "with dim as (select segment from raw) "
        "select * from facts f left join dim d on f.segment = d.segment"
    )
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_silent_when_predicate_is_disjunctive() -> None:
    # OR-disjunctions don't simplify to "join is on key X"; skip conservatively.
    parsed = _parse("select * from facts f left join dim d on f.id = d.id or f.alt = d.alt")
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_silent_when_predicate_has_function_call() -> None:
    parsed = _parse("select * from facts f left join dim d on lower(f.id) = lower(d.id)")
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_flagged_inside_cte_body() -> None:
    # Joins inside CTEs are equally susceptible to fanout, and the joined-in
    # side is still a ref'd model whose facts apply.
    parsed = _parse(
        "with widened as ("
        "  select * from facts f left join dim d on f.segment = d.segment"
        ") "
        "select * from widened"
    )
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert len(findings) == 1


def test_fanout_silent_when_cte_inherits_uniqueness_via_propagation() -> None:
    # The CTE `dim_local` pass-throughs `dim`, so its propagated key is `id`.
    # The join binds on `id`, so the join can't fan out — silent.
    parsed = _parse(
        "with dim_local as (select * from dim) "
        "select * from facts f join dim_local d on f.id = d.id"
    )
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert findings == ()


def test_fanout_flagged_when_join_to_propagated_cte_misses_inherited_key() -> None:
    # Same propagated key as above, but the join binds on `segment` rather
    # than the inherited `id` key. The detector now sees the CTE's facts
    # (no carve-out) and flags.
    parsed = _parse(
        "with dim_local as (select * from dim) "
        "select * from facts f join dim_local d on f.segment = d.segment"
    )
    findings = detect_join_fanout(
        parsed,
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.JOIN_FANOUT


def test_fanout_finding_carries_join_line() -> None:
    sql = "select *\nfrom facts f\nleft join dim d on f.segment = d.segment\n"
    findings = detect_join_fanout(
        _parse(sql),
        facts=_facts("model.pkg.dim", ("id",)),
        model_name_to_uid={"dim": "model.pkg.dim"},
    )
    assert len(findings) == 1
    assert findings[0].line_start == 3
