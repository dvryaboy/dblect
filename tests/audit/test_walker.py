"""Tests for the audit walker over a parsed manifest."""

from __future__ import annotations

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
    # jaffle's five models all carry raw_code from `dbt parse`, so none are skipped.
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


def test_default_detectors_covers_the_full_set() -> None:
    # Sanity: no detector silently dropped between the SQL layer and the audit layer.
    assert len(DEFAULT_DETECTORS) == 4


def test_located_finding_carries_file_path_for_every_scanned_model(
    jaffle: Manifest, jaffle_report: AuditReport
) -> None:
    expected_paths = {n.unique_id: n.original_file_path for n in jaffle.models.values()}
    for lf in jaffle_report.findings:
        assert lf.file_path == expected_paths[lf.model_unique_id]


def _without_raw_code(node: Node) -> Node:
    from dataclasses import replace

    return replace(node, raw_code=None)


def _with_raw_code(node: Node, sql: str) -> Node:
    from dataclasses import replace

    return replace(node, raw_code=sql)
