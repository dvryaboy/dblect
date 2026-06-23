"""The unified reporter renders both detector families in one report.

Text and JSON each carry the structural family (span-located, with snippet) and the
declaration family (model/column/contract) under one summary and one coverage block,
plus the audit-side suppressed/skipped blocks and the check-side unbuilt/load-issue
blocks. These pin the merged shape that replaced the two per-family reporters.
"""

from __future__ import annotations

import json

from dblect.analysis import AnalysisReport
from dblect.audit import AuditReport, LocatedFinding, SkippedModel, SuppressedFinding
from dblect.check.findings import CheckFinding, CheckFindingKind, CheckReport
from dblect.report import render_json, render_text
from dblect.sql import Finding, FindingKind, suppression_hint
from dblect.types import IssueCode

_MODEL = "model.p.m"


def _structural(
    message: str = "join can multiply rows",
    *,
    kind: FindingKind = FindingKind.JOIN_FANOUT,
) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id=_MODEL,
        file_path="models/m.sql",
        finding=Finding(
            kind=kind,
            message=message,
            sql_snippet="JOIN state ON e.id = s.id",
            line_start=9,
            line_end=9,
        ),
    )


def _declaration(message: str = "declared usd contradicted") -> CheckFinding:
    return CheckFinding(
        kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
        message=message,
        model_unique_id=_MODEL,
        file_path="models/m.sql",
        column="amount",
    )


def _report(
    *,
    structural: tuple[LocatedFinding, ...] = (),
    declaration: tuple[CheckFinding, ...] = (),
    suppressed: tuple[SuppressedFinding, ...] = (),
    skipped: tuple[SkippedModel, ...] = (),
) -> AnalysisReport:
    check = CheckReport(
        findings=declaration,
        load_issues=(),
        unbuilt=(),
        contracts_resolved=1,
        models_propagated=2,
        predicates_collected=0,
    )
    audit = AuditReport(
        findings=structural,
        suppressed=suppressed,
        skipped=skipped,
        models_scanned=2,
    )
    return AnalysisReport(findings=(*declaration, *structural), check=check, audit=audit)


def test_text_shows_both_families_under_one_report() -> None:
    text = render_text(_report(structural=(_structural(),), declaration=(_declaration(),)))
    assert "dblect: 2 findings over 2 models" in text
    assert "coverage:" in text
    # structural family: located block with the snippet
    assert "structural findings:" in text
    # join_fanout is an error-level correctness hazard; the level rides the head line.
    assert "L9  error  join_fanout" in text
    assert "snippet: JOIN state ON e.id = s.id" in text
    # declaration family: model.column block
    assert "declaration findings:" in text
    # domain_type_contradiction is an error-level family default.
    assert "error  domain_type_contradiction  model.p.m.amount" in text


def test_text_head_carries_the_issue_code_for_a_contract_issue() -> None:
    # A contract-issue head names its cause inline, so the reader sees an unsourced
    # field distinguished from any other contract issue without reading the body.
    issue = CheckFinding(
        kind=CheckFindingKind.CONTRACT_ISSUE,
        message="field 'currency' needs column 'currency', absent from the model",
        model_unique_id="model.p.orders",
        column="coupon_amount",
        contract="Orders",
        code=IssueCode.UNSOURCED_FIELD,
    )
    text = render_text(_report(declaration=(issue,)))
    assert "contract_issue (unsourced_field)  model.p.orders.coupon_amount" in text
    # the full explanation still rides the body
    assert "needs column 'currency'" in text


def test_text_head_omits_code_for_a_non_contract_issue() -> None:
    # A finding kind that carries no IssueCode renders its head with no code token,
    # never a stray `(None)`.
    text = render_text(_report(declaration=(_declaration(),)))
    assert "domain_type_contradiction" in text
    assert "domain_type_contradiction (" not in text


def test_text_singular_plural_and_clean_report() -> None:
    assert "1 finding over" in render_text(_report(structural=(_structural(),)))
    clean = render_text(_report())
    assert "0 findings over" in clean
    assert "structural findings:" not in clean
    assert "declaration findings:" not in clean


def test_text_carries_suppressed_and_skipped() -> None:
    suppressed = (
        SuppressedFinding(located=_structural(), reason="handled downstream", directive_line=8),
    )
    skipped = (SkippedModel(unique_id="model.p.x", reason="no compiled SQL"),)
    text = render_text(_report(suppressed=suppressed, skipped=skipped))
    assert "suppressed:" in text
    assert "-- handled downstream" in text
    assert "skipped:" in text
    assert "model.p.x  (no compiled SQL)" in text


def test_json_tags_each_finding_with_its_family() -> None:
    payload = json.loads(
        render_json(_report(structural=(_structural(),), declaration=(_declaration(),)))
    )
    assert payload["schema_version"] == "2"
    assert payload["summary"] == {
        "findings": 2,
        "structural": 1,
        "declaration": 1,
        "models_analyzed": 2,
        "models_scanned": 2,
        "contracts_resolved": 1,
        "predicates_collected": 0,
        "suppressed": 0,
        "skipped": 0,
        "load_issues": 0,
        "unbuilt": 0,
    }
    families = {f["family"]: f for f in payload["findings"]}
    assert families["structural"]["severity"] == "error"
    assert families["declaration"]["severity"] in {"info", "warn", "error"}
    assert families["structural"]["line_start"] == 9
    assert families["structural"]["sql_snippet"] == "JOIN state ON e.id = s.id"
    assert families["structural"]["column"] is None
    assert families["declaration"]["column"] == "amount"
    assert families["declaration"]["line_start"] is None
    # the check-family coverage block rides along
    assert "resolution" in payload["coverage"]
    assert payload["coverage"]["worlds"] == {"worlds_enumerated": 1, "axes_enumerated": []}


# --- the noqa-fixture suppression hint ---------------------------------------


def test_text_appends_suppression_hint_for_structural_findings() -> None:
    kind = FindingKind.JOIN_FANOUT
    text = render_text(_report(structural=(_structural(kind=kind),)))
    assert suppression_hint(kind) in text


def test_text_omits_hint_for_malformed_suppression() -> None:
    # Pointing a malformed suppression directive at the suppression syntax would be
    # circular, so this lone kind carries no hint.
    text = render_text(_report(structural=(_structural(kind=FindingKind.MALFORMED_SUPPRESSION),)))
    assert "noqa-fixture" not in text


def test_text_omits_hint_for_declaration_findings() -> None:
    # Declaration findings are not structural hazards; the suppression mechanism
    # does not apply, so the hint must not leak into their block.
    text = render_text(_report(declaration=(_declaration(),)))
    assert "noqa-fixture" not in text


def test_json_message_stays_the_pure_observation() -> None:
    # The hint is presentation: it rides the text reporter, never the JSON
    # `message`, which remains the detector's observation verbatim.
    observation = "ARRAY_AGG has no ORDER BY; element order across rows is undefined"
    structural = (_structural(message=observation, kind=FindingKind.UNORDERED_AGGREGATE),)
    payload = json.loads(render_json(_report(structural=structural)))
    [finding] = [f for f in payload["findings"] if f["family"] == "structural"]
    assert finding["message"] == observation
    assert "noqa-fixture" not in finding["message"]
