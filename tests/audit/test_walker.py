"""Tests for the audit walker over a parsed manifest."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dblect.audit import (
    DEFAULT_DETECTORS,
    AuditReport,
    SkippedModel,
    run_audit,
)
from dblect.manifest import Manifest, Node
from dblect.sql import FindingKind


@pytest.fixture(scope="module")
def jaffle(jaffle_manifest_path: Path) -> Manifest:
    return Manifest.from_file(jaffle_manifest_path)


@pytest.fixture(scope="module")
def jaffle_report(jaffle: Manifest) -> AuditReport:
    return run_audit(jaffle)


def test_audit_scans_every_model_with_raw_code(jaffle: Manifest, jaffle_report: AuditReport) -> None:
    # Every jaffle model carries raw_code from `dbt parse`, so none are skipped.
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


def test_audit_skips_models_without_raw_code(jaffle: Manifest) -> None:
    # Drop raw_code on one model to simulate the source/seed shape.
    [a_model_uid] = list(jaffle.models)[:1]
    drained = dict(jaffle.nodes)
    drained[a_model_uid] = _without_raw_code(drained[a_model_uid])
    altered = Manifest(schema_version=jaffle.schema_version, nodes=drained)
    report = run_audit(altered)
    assert SkippedModel(unique_id=a_model_uid, reason="no raw_code") in report.skipped
    assert report.models_scanned == len(jaffle.models) - 1


def test_audit_records_parse_errors_without_blowing_up(jaffle: Manifest) -> None:
    # Replace one model's SQL with something sqlglot can't parse.
    [a_model_uid] = list(jaffle.models)[:1]
    drained = dict(jaffle.nodes)
    drained[a_model_uid] = _with_raw_code(drained[a_model_uid], "select from where")
    altered = Manifest(schema_version=jaffle.schema_version, nodes=drained)
    report = run_audit(altered)
    assert any(
        s.unique_id == a_model_uid and s.reason.startswith("parse error") for s in report.skipped
    )
    # Other models still got scanned.
    assert report.models_scanned == len(jaffle.models) - 1


def test_custom_detector_list_only_runs_those(jaffle: Manifest) -> None:
    # Empty detector tuple = nothing fires; still exercises the parse/scan loop.
    report = run_audit(jaffle, detectors=())
    assert report.findings == ()
    assert report.models_scanned == len(jaffle.models)


def test_counts_by_kind_matches_findings(jaffle_report: AuditReport) -> None:
    actual = jaffle_report.counts_by_kind
    expected: dict[FindingKind, int] = {}
    for lf in jaffle_report.findings:
        expected[lf.finding.kind] = expected.get(lf.finding.kind, 0) + 1
    assert actual == expected


def test_default_detectors_includes_every_sql_detector_exactly_once() -> None:
    # The audit layer must wire in every `detect_*` the SQL layer exports.
    # If a new detector lands in dblect.sql but the walker forgets to add it
    # to DEFAULT_DETECTORS, audits would silently skip it; this test catches
    # that without pinning the count.
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
        nodes={
            **jaffle.nodes,
            customers.unique_id: replace(customers, raw_code=suppressed_sql),
        },
    )
    report = run_audit(altered)
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
        nodes={
            **jaffle.nodes,
            a_model_uid: replace(node, raw_code="-- noqa-fixture\n" + node.raw_code),
        },
    )
    report = run_audit(altered)
    [bad] = [
        lf
        for lf in report.findings
        if lf.model_unique_id == a_model_uid
        and lf.finding.kind.value == "malformed_suppression"
    ]
    assert bad.finding.line_start == 1


def test_unsuppressed_findings_in_other_models_still_fire(jaffle: Manifest) -> None:
    # Suppressing in customers.sql doesn't affect detection elsewhere.
    customers = jaffle.nodes["model.jaffle_shop.customers"]
    assert customers.raw_code is not None
    blanket = "-- noqa-fixture: silence everything in this file\n" + customers.raw_code
    altered = Manifest(
        schema_version=jaffle.schema_version,
        nodes={
            **jaffle.nodes,
            customers.unique_id: replace(customers, raw_code=blanket),
        },
    )
    report = run_audit(altered)
    # The blanket suppression at line 1 only covers line 1-2, not the GROUP BY.
    # So the customers null-group finding still surfaces.
    still_there = [
        lf for lf in report.findings if lf.model_unique_id == customers.unique_id
    ]
    assert still_there, (
        "A line-1 directive should not blanket-suppress findings on far-down lines"
    )
    # No SuppressedFinding objects were produced from customers (the directive
    # didn't actually match anything).
    assert not any(
        s.located.model_unique_id == customers.unique_id for s in report.suppressed
    ), "Line-1 directive matched nothing, so suppressed should be empty for customers"


def _without_raw_code(node: Node) -> Node:
    return replace(node, raw_code=None)


def _with_raw_code(node: Node, sql: str) -> Node:
    return replace(node, raw_code=sql)
