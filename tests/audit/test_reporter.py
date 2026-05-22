"""Tests for the text reporter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dblect.audit import AuditReport, LocatedFinding, SkippedModel, SuppressedFinding, run_audit
from dblect.audit.reporter import JSON_SCHEMA_VERSION, render_json, render_text
from dblect.manifest import Manifest
from dblect.sql import Finding, FindingKind


def _finding(
    line_start: int = 5,
    line_end: int | None = None,
    *,
    kind: FindingKind = FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
    message: str = "an issue",
    snippet: str = "x",
) -> Finding:
    return Finding(
        kind=kind,
        message=message,
        sql_snippet=snippet,
        line_start=line_start,
        line_end=line_end if line_end is not None else line_start,
    )


def _lf(
    uid: str = "model.pkg.a",
    file_path: str | None = "models/a.sql",
    finding: Finding | None = None,
) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id=uid,
        file_path=file_path,
        finding=finding or _finding(),
    )


def test_empty_report_prints_only_summary() -> None:
    report = AuditReport(findings=(), suppressed=(), skipped=(), models_scanned=3)
    text = render_text(report)
    assert "scanned 3 models" in text
    assert "0 findings" in text


def test_summary_pluralization() -> None:
    one = AuditReport(findings=(_lf(),), suppressed=(), skipped=(), models_scanned=1)
    assert "1 finding" in render_text(one)
    assert "1 findings" not in render_text(one)


def test_findings_render_grouped_by_model_with_file_path() -> None:
    a = _lf("model.pkg.a", "models/a.sql", _finding(line_start=10, line_end=12))
    b1 = _lf("model.pkg.b", "models/b.sql", _finding(line_start=4))
    b2 = _lf("model.pkg.b", "models/b.sql", _finding(line_start=20))
    report = AuditReport(
        findings=(b2, a, b1),  # unsorted on purpose
        suppressed=(),
        skipped=(),
        models_scanned=2,
    )
    text = render_text(report)
    # Models sorted by unique_id: a before b.
    a_idx = text.index("models/a.sql")
    b_idx = text.index("models/b.sql")
    assert a_idx < b_idx
    # Within a model, sorted by line_start: line 4 before line 20.
    b_chunk = text[b_idx:]
    assert b_chunk.index("L4") < b_chunk.index("L20")
    # Multi-line range renders as L10-12.
    assert "L10-12" in text


def test_finding_with_no_line_renders_as_question() -> None:
    lf = _lf(finding=_finding(line_start=0, line_end=0))
    report = AuditReport(findings=(lf,), suppressed=(), skipped=(), models_scanned=1)
    text = render_text(report)
    assert "L?" in text


def test_suppressed_block_shows_reason() -> None:
    suppressed = SuppressedFinding(
        located=_lf(finding=_finding(line_start=7)),
        reason="orphan handling",
        directive_line=7,
    )
    report = AuditReport(findings=(), suppressed=(suppressed,), skipped=(), models_scanned=1)
    text = render_text(report)
    assert "suppressed:" in text
    assert "orphan handling" in text
    assert "1 suppressed" in text


def test_skipped_block_shows_reasons() -> None:
    skipped = (
        SkippedModel(unique_id="model.pkg.bad", reason="parse error: ..."),
        SkippedModel(unique_id="source.pkg.s", reason="no compiled SQL"),
    )
    report = AuditReport(findings=(), suppressed=(), skipped=skipped, models_scanned=0)
    text = render_text(report)
    assert "skipped:" in text
    assert "model.pkg.bad" in text
    assert "parse error" in text
    assert "2 skipped" in text


def test_report_against_real_jaffle_manifest(jaffle_manifest_path: Path) -> None:
    manifest = Manifest.from_file(jaffle_manifest_path)
    report = run_audit(manifest)
    text = render_text(report)
    # The customers null-group hit is the one we know jaffle has.
    assert "models/customers.sql" in text
    assert "null_group_after_outer_join" in text
    assert text.endswith("\n")


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [(0, 0, "L?"), (5, 5, "L5"), (5, 7, "L5-7")],
)
def test_line_range_formatting(start: int, end: int, expected: str) -> None:
    lf = _lf(finding=_finding(line_start=start, line_end=end))
    report = AuditReport(findings=(lf,), suppressed=(), skipped=(), models_scanned=1)
    assert expected in render_text(report)


# --- JSON reporter ---


def test_json_empty_report_has_well_formed_summary() -> None:
    report = AuditReport(findings=(), suppressed=(), skipped=(), models_scanned=3)
    payload = json.loads(render_json(report))
    assert payload["schema_version"] == JSON_SCHEMA_VERSION
    assert payload["summary"] == {
        "models_scanned": 3,
        "findings": 0,
        "suppressed": 0,
        "skipped": 0,
    }
    assert payload["findings"] == []
    assert payload["suppressed"] == []
    assert payload["skipped"] == []


def test_json_finding_has_documented_shape() -> None:
    lf = _lf(
        "model.pkg.a",
        "models/a.sql",
        _finding(
            line_start=10,
            line_end=12,
            kind=FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
            message="m",
            snippet="s",
        ),
    )
    report = AuditReport(findings=(lf,), suppressed=(), skipped=(), models_scanned=1)
    payload = json.loads(render_json(report))
    [f] = payload["findings"]
    assert f == {
        "model_unique_id": "model.pkg.a",
        "file_path": "models/a.sql",
        "kind": "null_group_after_outer_join",
        "line_start": 10,
        "line_end": 12,
        "message": "m",
        "sql_snippet": "s",
    }


def test_json_suppressed_finding_includes_directive() -> None:
    s = SuppressedFinding(
        located=_lf(finding=_finding(line_start=7)),
        reason="orphan handling",
        directive_line=7,
    )
    report = AuditReport(findings=(), suppressed=(s,), skipped=(), models_scanned=1)
    payload = json.loads(render_json(report))
    [entry] = payload["suppressed"]
    assert entry["suppression"] == {"reason": "orphan handling", "directive_line": 7}
    # The finding fields are still present alongside the suppression block.
    assert entry["kind"] == "null_group_after_outer_join"


def test_json_skipped_models_round_trip() -> None:
    report = AuditReport(
        findings=(),
        suppressed=(),
        skipped=(SkippedModel(unique_id="model.pkg.bad", reason="parse error: ..."),),
        models_scanned=0,
    )
    payload = json.loads(render_json(report))
    assert payload["skipped"] == [
        {"unique_id": "model.pkg.bad", "reason": "parse error: ..."}
    ]


def test_json_is_stable_under_unsorted_input() -> None:
    a = _lf("model.pkg.a", "models/a.sql", _finding(line_start=10))
    b = _lf("model.pkg.b", "models/b.sql", _finding(line_start=5))
    r1 = AuditReport(findings=(a, b), suppressed=(), skipped=(), models_scanned=2)
    r2 = AuditReport(findings=(b, a), suppressed=(), skipped=(), models_scanned=2)
    # JSON ordering follows the input findings order, NOT a sort, so the two
    # reports are different documents. This test pins that contract so we
    # don't accidentally reorder later.
    assert render_json(r1) != render_json(r2)


def test_json_against_real_jaffle(jaffle_manifest_path: Path) -> None:
    manifest = Manifest.from_file(jaffle_manifest_path)
    report = run_audit(manifest)
    payload = json.loads(render_json(report))
    assert payload["summary"]["models_scanned"] == len(manifest.models)
    kinds = {f["kind"] for f in payload["findings"]}
    assert "null_group_after_outer_join" in kinds
