"""The SARIF reporter's output is valid SARIF 2.1.0 and loses no finding.

The one contract worth testing on a report format is that consumers can read it, so we
validate against the official SARIF 2.1.0 schema rather than restate our own JSON
shape. The rest is coverage: every finding reaches the document, a suppressed finding
is marked suppressed rather than dropped, and the CLI emits the same valid document.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from dblect.adapters import profile_for_adapter
from dblect.analysis import AnalysisReport, analyze
from dblect.audit import AuditReport, LocatedFinding, SkippedModel, SuppressedFinding
from dblect.check.findings import CheckFinding, CheckFindingKind, CheckReport, UnbuiltModel
from dblect.loader import LoadIssue
from dblect.manifest import Manifest
from dblect.sarif import SARIF_VERSION, render_sarif
from dblect.sql import Finding, FindingKind
from dblect.types import IssueCode

_SCHEMA_PATH = Path(__file__).parent / "fixtures" / "sarif" / "sarif-2.1.0.schema.json"
_VERSION = "9.9.9"
_MODEL = "model.p.m"


@cache
def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text())


def _validate(sarif_text: str) -> dict[str, Any]:
    document = json.loads(sarif_text)
    jsonschema.validate(instance=document, schema=_schema())
    return document


def _located(*, line: int) -> LocatedFinding:
    # line 0 is the detector's "could not pin a line" sentinel.
    return LocatedFinding(
        model_unique_id=_MODEL,
        file_path="models/m.sql",
        finding=Finding(
            kind=FindingKind.JOIN_FANOUT,
            message="join can multiply rows",
            sql_snippet="JOIN s ON e.id = s.id",
            line_start=line,
            line_end=line,
        ),
    )


def _declaration(*, column: str | None) -> CheckFinding:
    return CheckFinding(
        kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
        message="declared usd contradicted",
        model_unique_id=_MODEL,
        file_path="models/m.sql",
        column=column,
    )


def _contract_issue() -> CheckFinding:
    return CheckFinding(
        kind=CheckFindingKind.CONTRACT_ISSUE,
        message="field 'currency' needs column 'currency', absent from the model",
        model_unique_id=None,
        column="coupon_amount",
        contract="Orders",
        code=IssueCode.UNSOURCED_FIELD,
    )


def _every_branch_report() -> AnalysisReport:
    """A report touching each shape the reporter emits: a located and an unlocated
    structural finding, a declaration finding with and without a column, a suppressed
    finding, and each kind of unanalyzed-model notification."""
    structural = (_located(line=9), _located(line=0))
    declaration = (_declaration(column="amount"), _declaration(column=None), _contract_issue())
    check = CheckReport(
        findings=declaration,
        load_issues=(LoadIssue(module="dblect.contracts.orders", message="ImportError"),),
        unbuilt=(UnbuiltModel(unique_id="model.p.y", reason="parse error"),),
        contracts_resolved=1,
        models_propagated=2,
        predicates_collected=0,
    )
    audit = AuditReport(
        findings=structural,
        suppressed=(
            SuppressedFinding(located=_located(line=9), reason="handled", directive_line=8),
        ),
        skipped=(SkippedModel(unique_id="model.p.x", reason="no compiled SQL"),),
        models_scanned=2,
    )
    return AnalysisReport(findings=(*declaration, *structural), check=check, audit=audit)


def test_every_emitted_shape_validates_against_the_sarif_schema() -> None:
    _validate(render_sarif(_every_branch_report(), version=_VERSION))


def test_contract_issue_rule_id_subnamespaces_by_code_and_carries_it() -> None:
    # Each contract-issue cause gets a stable, distinct ruleId so code scanning can
    # group and triage by cause; the code also rides as a machine-readable property.
    audit = AuditReport(findings=(), suppressed=(), skipped=(), models_scanned=1)
    check = CheckReport(
        findings=(_contract_issue(),),
        load_issues=(),
        unbuilt=(),
        contracts_resolved=1,
        models_propagated=1,
        predicates_collected=0,
    )
    report = AnalysisReport(findings=(_contract_issue(),), check=check, audit=audit)

    run = _validate(render_sarif(report, version=_VERSION))["runs"][0]
    (result,) = run["results"]
    assert result["ruleId"] == "declaration/contract_issue/unsourced_field"
    assert result["properties"]["code"] == "unsourced_field"
    assert "declaration/contract_issue/unsourced_field" in {
        r["id"] for r in run["tool"]["driver"]["rules"]
    }


def test_codeless_declaration_finding_keeps_its_bare_rule_id() -> None:
    # A finding kind with no IssueCode keeps the family/kind ruleId and grows no code
    # property, so only contract issues are sub-namespaced.
    audit = AuditReport(findings=(), suppressed=(), skipped=(), models_scanned=1)
    check = CheckReport(
        findings=(_declaration(column="amount"),),
        load_issues=(),
        unbuilt=(),
        contracts_resolved=1,
        models_propagated=1,
        predicates_collected=0,
    )
    report = AnalysisReport(findings=(_declaration(column="amount"),), check=check, audit=audit)

    (result,) = _validate(render_sarif(report, version=_VERSION))["runs"][0]["results"]
    assert result["ruleId"] == "declaration/domain_type_contradiction"
    assert "properties" not in result


def test_jaffle_output_validates_against_the_sarif_schema(jaffle_manifest_path: Path) -> None:
    report = analyze(Manifest.from_file(jaffle_manifest_path), profile_for_adapter("duckdb"))
    assert report.findings, "jaffle is expected to surface at least one finding to render"
    _validate(render_sarif(report, version=_VERSION))


def test_no_finding_is_dropped() -> None:
    report = _every_branch_report()
    run = _validate(render_sarif(report, version=_VERSION))["runs"][0]

    # Every active and suppressed finding becomes a result; nothing invented.
    assert len(run["results"]) == len(report.findings) + len(report.audit.suppressed)
    # Every model the analysis could not read becomes a notification.
    notifications = run["invocations"][0]["toolExecutionNotifications"]
    assert len(notifications) == (
        len(report.audit.skipped) + len(report.check.unbuilt) + len(report.check.load_issues)
    )


def test_suppressed_finding_is_marked_suppressed_not_dropped() -> None:
    suppressed = (SuppressedFinding(located=_located(line=9), reason="handled", directive_line=8),)
    audit = AuditReport(findings=(), suppressed=suppressed, skipped=(), models_scanned=1)
    check = CheckReport(
        findings=(),
        load_issues=(),
        unbuilt=(),
        contracts_resolved=0,
        models_propagated=1,
        predicates_collected=0,
    )
    report = AnalysisReport(findings=(), check=check, audit=audit)

    (result,) = _validate(render_sarif(report, version=_VERSION))["runs"][0]["results"]
    # A suppressed finding still reaches code scanning, marked so it is not re-alarmed.
    assert result["suppressions"][0]["justification"] == "handled"


@pytest.mark.parametrize("fmt", ["text", "json", "sarif"])
def test_cli_check_emits_requested_format(jaffle_manifest_path: Path, fmt: str) -> None:
    from typer.testing import CliRunner

    from dblect.cli import app

    result = CliRunner().invoke(
        app, ["check", "--manifest", str(jaffle_manifest_path), "--format", fmt, "--no-fail"]
    )
    assert result.exit_code == 0, result.output
    if fmt == "sarif":
        doc = _validate(result.stdout)  # diagnostics go to stderr; stdout is the document
        assert doc["version"] == SARIF_VERSION
