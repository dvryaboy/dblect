"""Tests for the audit walker over a parsed manifest."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dblect.adapters import AdapterProfile, profile_for_adapter
from dblect.audit import (
    DEFAULT_DETECTORS,
    AuditReport,
    run_audit,
)
from dblect.manifest import Manifest, Node, ResourceType
from dblect.sql import FindingKind

_DUCKDB = profile_for_adapter("duckdb")


@pytest.fixture(scope="module")
def jaffle(jaffle_manifest_path: Path) -> Manifest:
    return Manifest.from_file(jaffle_manifest_path)


@pytest.fixture(scope="module")
def jaffle_report(jaffle: Manifest) -> AuditReport:
    return run_audit(jaffle, _DUCKDB)


def test_audit_scans_every_model_with_compiled_code(
    jaffle: Manifest, jaffle_report: AuditReport
) -> None:
    # Every jaffle model carries compiled_code from `dbt compile`, so none are
    # skipped.
    assert jaffle_report.models_scanned == len(jaffle.models)
    assert jaffle_report.skipped == ()


def test_audit_locates_customers_null_group(jaffle_report: AuditReport) -> None:
    hits = [
        lf
        for lf in jaffle_report.findings
        if lf.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
        and lf.model_unique_id == "model.jaffle_shop.customers"
    ]
    assert len(hits) == 1
    [hit] = hits
    assert hit.file_path == "models/customers.sql"
    assert hit.finding.line_start > 0
    assert hit.finding.line_start <= hit.finding.line_end


def test_audit_skips_models_without_sql(jaffle: Manifest) -> None:
    # Drop both raw_code and compiled_code on one model to simulate the
    # source/seed shape.
    [a_model_uid] = list(jaffle.models)[:1]
    drained = dict(jaffle.nodes)
    drained[a_model_uid] = _without_sql(drained[a_model_uid])
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes=drained,
    )
    report = run_audit(altered, _DUCKDB)
    assert any(
        s.unique_id == a_model_uid and s.reason.startswith("no compiled SQL")
        for s in report.skipped
    )
    assert report.models_scanned == len(jaffle.models) - 1


def test_audit_records_parse_errors_without_blowing_up(jaffle: Manifest) -> None:
    # Replace one model's SQL with something sqlglot can't parse.
    [a_model_uid] = list(jaffle.models)[:1]
    drained = dict(jaffle.nodes)
    drained[a_model_uid] = _with_compiled_code(drained[a_model_uid], "select from where")
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes=drained,
    )
    report = run_audit(altered, _DUCKDB)
    assert any(
        s.unique_id == a_model_uid and s.reason.startswith("parse error") for s in report.skipped
    )
    # Other models still got scanned.
    assert report.models_scanned == len(jaffle.models) - 1


def test_empty_detector_list_gates_the_structural_detectors(jaffle: Manifest) -> None:
    # The `detectors` arg gates the structural detectors: an empty tuple drops them,
    # so jaffle's structural findings disappear, while the scan loop still runs over
    # every model. The context-bound detectors (non-determinism, uniqueness,
    # nullability) are built in run_audit and not gated by this arg.
    structural = {
        FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
        FindingKind.COALESCE_ON_JOIN_KEY,
        FindingKind.UNORDERED_RANKING_WINDOW,
        FindingKind.UNORDERED_AGGREGATE,
        FindingKind.WHERE_ON_OUTER_JOINED_NULLABLE,
    }
    full_kinds = {lf.finding.kind for lf in run_audit(jaffle, _DUCKDB).findings}
    assert full_kinds & structural, "jaffle should exercise at least one structural detector"

    gated = run_audit(jaffle, _DUCKDB, detectors=())
    assert gated.models_scanned == len(jaffle.models)
    assert {lf.finding.kind for lf in gated.findings}.isdisjoint(structural)


def test_counts_by_kind_matches_findings(jaffle_report: AuditReport) -> None:
    actual = jaffle_report.counts_by_kind
    expected: dict[FindingKind, int] = {}
    for lf in jaffle_report.findings:
        expected[lf.finding.kind] = expected.get(lf.finding.kind, 0) + 1
    assert actual == expected


def test_default_detectors_includes_every_sql_detector_exactly_once() -> None:
    # The audit layer must wire in every structural `detect_*` the SQL layer
    # exports. If a new one lands in dblect.sql but the walker forgets to add it to
    # DEFAULT_DETECTORS, audits would silently skip it; this test catches that
    # without pinning the count. Context-bound detectors are `make_*` factories
    # (e.g. make_non_determinism_detector), built per run in run_audit, so they sit
    # outside this guard by design.
    import dblect.sql as sql_module

    sql_detectors = {
        getattr(sql_module, name)
        for name in dir(sql_module)
        if name.startswith("detect_") and callable(getattr(sql_module, name))
    }
    default_set = set(DEFAULT_DETECTORS)
    assert sql_detectors == default_set
    assert len(DEFAULT_DETECTORS) == len(default_set), "DEFAULT_DETECTORS contains duplicates"


def test_located_finding_carries_file_path_for_every_scanned_model(
    jaffle: Manifest, jaffle_report: AuditReport
) -> None:
    expected_paths = {n.unique_id: n.original_file_path for n in jaffle.models.values()}
    for lf in jaffle_report.findings:
        assert lf.file_path == expected_paths[lf.model_unique_id]


def test_suppression_silences_matching_finding_end_to_end(jaffle: Manifest) -> None:
    # Prepend a top-level noqa-fixture so the customers null-group finding is
    # silenced (the directive applies to the line above any finding too, but
    # the simplest demonstration is to put it on the offending line by appending
    # to customers.sql's GROUP BY line). Instead of touching the SQL string,
    # we use a kind-specific directive on the very first line; it has to share
    # a line with the finding to apply, so just put it on the GROUP BY's line.
    customers = jaffle.nodes["model.jaffle_shop.customers"]
    assert customers.raw_code is not None
    suppressed_sql = customers.raw_code.replace(
        "group by orders.customer_id",
        "group by orders.customer_id -- noqa-fixture: null_group_after_outer_join: orphan handling",
    )
    assert suppressed_sql != customers.raw_code, "test setup failed to find target line"
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes={
            **jaffle.nodes,
            customers.unique_id: replace(customers, raw_code=suppressed_sql),
        },
    )
    report = run_audit(altered, _DUCKDB)
    # The customers null-group finding is gone from active findings.
    null_group_hits = [
        lf
        for lf in report.findings
        if lf.model_unique_id == customers.unique_id
        and lf.finding.kind.value == "null_group_after_outer_join"
    ]
    assert null_group_hits == []
    # And it shows up in suppressed with the reason preserved.
    suppressed_hits = [
        s for s in report.suppressed if s.located.model_unique_id == customers.unique_id
    ]
    assert any(s.reason == "orphan handling" for s in suppressed_hits)


def test_bare_noqa_fixture_surfaces_as_malformed_suppression(jaffle: Manifest) -> None:
    a_model_uid = "model.jaffle_shop.stg_customers"
    node = jaffle.nodes[a_model_uid]
    assert node.raw_code is not None
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes={
            **jaffle.nodes,
            a_model_uid: replace(node, raw_code="-- noqa-fixture\n" + node.raw_code),
        },
    )
    report = run_audit(altered, _DUCKDB)
    [bad] = [
        lf
        for lf in report.findings
        if lf.model_unique_id == a_model_uid and lf.finding.kind.value == "malformed_suppression"
    ]
    assert bad.finding.line_start == 1


def test_unsuppressed_findings_in_other_models_still_fire(jaffle: Manifest) -> None:
    # Suppressing in customers.sql doesn't affect detection elsewhere.
    customers = jaffle.nodes["model.jaffle_shop.customers"]
    assert customers.raw_code is not None
    blanket = "-- noqa-fixture: silence everything in this file\n" + customers.raw_code
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes={
            **jaffle.nodes,
            customers.unique_id: replace(customers, raw_code=blanket),
        },
    )
    report = run_audit(altered, _DUCKDB)
    # The blanket suppression at line 1 only covers line 1-2, not the GROUP BY.
    # So the customers null-group finding still surfaces.
    still_there = [lf for lf in report.findings if lf.model_unique_id == customers.unique_id]
    assert still_there, "A line-1 directive should not blanket-suppress findings on far-down lines"
    # No SuppressedFinding objects were produced from customers (the directive
    # didn't actually match anything).
    assert not any(s.located.model_unique_id == customers.unique_id for s in report.suppressed), (
        "Line-1 directive matched nothing, so suppressed should be empty for customers"
    )


def test_run_audit_parses_each_model_once(
    jaffle: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The facts pre-pass and the per-model detector loop used to each parse
    # every model's compiled SQL, which was twice the work. The walker now
    # parses once and shares the trees. Lock that in by counting calls to
    # sqlglot.parse_one against each model's compiled SQL.
    from typing import Any

    import sqlglot

    model_sqls = {m.compiled_code for m in jaffle.models.values() if m.compiled_code is not None}
    counts: dict[str, int] = {}
    real_parse_one = sqlglot.parse_one

    def counting_parse_one(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql in model_sqls:
            counts[sql] = counts.get(sql, 0) + 1
        return real_parse_one(sql, *args, **kwargs)  # pyright: ignore[reportUnknownVariableType]

    monkeypatch.setattr(sqlglot, "parse_one", counting_parse_one)
    run_audit(jaffle, _DUCKDB)
    assert set(counts) == model_sqls, "every model's compiled SQL should be parsed"
    repeated = {n for n in counts.values() if n > 1}
    assert not repeated, f"each model's SQL should parse exactly once; counts: {counts.values()}"


def test_macro_emitted_join_visible_in_compiled_code() -> None:
    # Regression guard for "we analyze compiled_code". A model whose LEFT
    # JOIN comes from a macro call: in the on-disk template the join is
    # invisible (`{{ join_country(u) }}`), but the compiled SQL has the
    # join expanded inline, so the null-group-after-outer-join detector
    # sees it.
    compiled_sql = (
        "select u.user_id, d.country, count(*) as n\n"
        "from users u\n"
        "left join dim_country d on u.country_code = d.code\n"
        "group by u.user_id, d.country"
    )
    node = Node(
        unique_id="model.pkg.user_country",
        name="user_country",
        resource_type=ResourceType.MODEL,
        fqn=("pkg", "user_country"),
        package_name="pkg",
        schema=None,
        raw_code=(
            "select u.user_id, d.country, count(*) as n\n"
            "from users u\n"
            "{{ join_country(u) }}\n"
            "group by u.user_id, d.country"
        ),
        compiled_code=compiled_sql,
        original_file_path="models/user_country.sql",
        columns={},
    )
    manifest = Manifest(schema_version="x", adapter_type="duckdb", nodes={node.unique_id: node})
    report = run_audit(manifest, _DUCKDB)
    assert any(
        lf.finding.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN for lf in report.findings
    ), "compiled path should see the macro-emitted LEFT JOIN and flag the GROUP BY"


def test_resolved_adapter_reaches_non_determinism_detector(jaffle: Manifest) -> None:
    # A DuckDB-only nondeterministic builtin in a load-bearing position fires under the
    # duckdb profile and stays silent under another, proving the audit's resolved
    # adapter's `non_deterministic_builtins` reach the non-determinism detector.
    # txid_current() parses as exp.Anonymous in every dialect, so the resolved
    # adapter's name set alone decides whether it fires.
    [uid] = list(jaffle.models)[:1]
    nodes = dict(jaffle.nodes)
    nodes[uid] = _with_compiled_code(nodes[uid], "select * from a join b on a.k = txid_current()")
    altered = Manifest(
        schema_version=jaffle.schema_version,
        adapter_type=jaffle.adapter_type,
        nodes=nodes,
    )

    def nondet_hits(profile: AdapterProfile) -> int:
        report = run_audit(altered, profile)
        return sum(
            lf.model_unique_id == uid and lf.finding.kind is FindingKind.NON_DETERMINISTIC_FUNCTION
            for lf in report.findings
        )

    assert nondet_hits(profile_for_adapter("duckdb")) == 1
    assert nondet_hits(profile_for_adapter("snowflake")) == 0


def _without_sql(node: Node) -> Node:
    return replace(node, raw_code=None, compiled_code=None)


def _with_compiled_code(node: Node, sql: str) -> Node:
    return replace(node, compiled_code=sql)
