"""Tests for the window order-keys uniqueness detector."""

from __future__ import annotations

from collections.abc import Mapping

from dblect.sql import FindingKind, ParsedSQL
from dblect.uniqueness import UniquenessFact, UniquenessSource
from dblect.uniqueness.detector import detect_non_unique_window_order_keys


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


def _parse(sql: str) -> ParsedSQL:
    return ParsedSQL.parse(sql, dialect="duckdb")


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
    parsed = _parse(
        "select row_number() over (partition by customer_id order by ts) from src"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("customer_id", "ts")),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert findings == ()


def test_superkey_covered_by_subset_fact_is_silent() -> None:
    # `id` alone is unique on the source; (id, ts) is a superkey and still unique.
    parsed = _parse(
        "select row_number() over (partition by id order by ts) from src"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert findings == ()


def test_no_order_by_window_is_not_flagged() -> None:
    # detect_unordered_window covers this case; we don't double-flag.
    parsed = _parse(
        "select row_number() over (partition by customer_id) from src"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert findings == ()


def test_no_facts_for_source_stays_silent() -> None:
    # We don't know if the source has unique keys, so we can't claim a hazard.
    parsed = _parse(
        "select row_number() over (partition by customer_id order by ts) from src"
    )
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


def test_window_inside_with_clause_body_is_inspected() -> None:
    parsed = _parse(
        "with src as (select * from raw) "
        "select row_number() over (partition by customer_id order by ts) from src"
    )
    findings = detect_non_unique_window_order_keys(
        parsed,
        # Note: the FROM references the CTE `src`, not the ref'd model. We
        # have a model `raw`; the CTE shadows it. Conservatively this stays
        # silent because `src` resolves to the CTE, not the model.
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
    sql = (
        "select\n"
        "  row_number() over (partition by customer_id order by ts) as rn\n"
        "from src\n"
    )
    findings = detect_non_unique_window_order_keys(
        _parse(sql),
        facts=_facts("model.pkg.src", ("id",)),
        model_name_to_uid={"src": "model.pkg.src"},
    )
    assert len(findings) == 1
    # The window lives on line 2.
    assert findings[0].line_start == 2
