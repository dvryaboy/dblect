"""The unified reporter renders both detector families in one report.

Text and JSON each carry the structural family (span-located, with snippet) and the
declaration family (model/column/contract) under one summary and one coverage block,
plus the audit-side suppressed/skipped blocks and the check-side unbuilt/load-issue
blocks. These pin the merged shape that replaced the two per-family reporters.
"""

from __future__ import annotations

import json

from dblect.analysis import AnalysisReport
from dblect.audit import (
    AuditReport,
    LocatedFinding,
    SkippedModel,
    SourceSpan,
    SpanBasis,
    SuppressedFinding,
)
from dblect.check.findings import CheckFinding, CheckFindingKind, CheckReport
from dblect.report import render_json, render_text
from dblect.sql import Finding, FindingKind, suppression_hint
from dblect.types import IssueCode

_MODEL = "model.p.m"


def _structural(
    message: str = "join can multiply rows",
    *,
    kind: FindingKind = FindingKind.JOIN_FANOUT,
    source_span: SourceSpan | None = None,
) -> LocatedFinding:
    # Default to a successful back-map (source line == compiled line), the common
    # ref-only case; tests that need the compiled-relative path pass it explicitly.
    if source_span is None:
        source_span = SourceSpan(9, 9, SpanBasis.SOURCE)
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
        source_span=source_span,
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
    suppressed = (SuppressedFinding(located=_structural(), directive_line=8, bare=False),)
    skipped = (SkippedModel(unique_id="model.p.x", reason="no compiled SQL"),)
    text = render_text(_report(suppressed=suppressed, skipped=skipped))
    assert "suppressed:" in text
    # A code-specific suppression names how it was silenced and the directive's line.
    assert "suppressed by noqa: DBLECT_JOIN_FANOUT @ L8" in text
    assert "skipped:" in text
    assert "model.p.x  (no compiled SQL)" in text


def test_text_suppressed_block_shows_bare_noqa() -> None:
    suppressed = (SuppressedFinding(located=_structural(), directive_line=4, bare=True),)
    text = render_text(_report(suppressed=suppressed))
    assert "suppressed by noqa @ L4" in text


def test_text_marks_a_compiled_frame_directive_line() -> None:
    # A macro body's `-- noqa` is matched in compiled space, so its line is labelled
    # `compiled L<n>` and never mistaken for a line in the developer's template.
    suppressed = (
        SuppressedFinding(
            located=_structural(), directive_line=5, bare=False, directive_in_compiled=True
        ),
    )
    text = render_text(_report(suppressed=suppressed))
    assert "suppressed by noqa: DBLECT_JOIN_FANOUT @ compiled L5" in text


def test_json_tags_each_finding_with_its_family() -> None:
    payload = json.loads(
        render_json(_report(structural=(_structural(),), declaration=(_declaration(),)))
    )
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


def test_structural_finding_carries_back_mapped_source_span() -> None:
    # A back-mapped finding reports its compiled span unchanged and a source span the
    # text renderer shows without a marker; the JSON records the basis as source.
    text = render_text(_report(structural=(_structural(),)))
    assert "L9  error  join_fanout" in text
    assert "(compiled)" not in text
    payload = json.loads(render_json(_report(structural=(_structural(),))))
    [f] = [f for f in payload["findings"] if f["family"] == "structural"]
    assert (f["line_start"], f["source_line_start"], f["line_basis"]) == (9, 9, "source")


def test_compiled_relative_finding_is_marked_and_keeps_compiled_line() -> None:
    # A finding whose compiled span could not be back-mapped reports the compiled
    # line, marked so a reader knows it indexes the compiled SQL, not the source file.
    compiled_only = _structural(source_span=SourceSpan(12, 12, SpanBasis.COMPILED))

    # rebind the compiled span to match the fallback the walker would produce
    def _at(line: int) -> LocatedFinding:
        f = compiled_only.finding
        return LocatedFinding(
            model_unique_id=compiled_only.model_unique_id,
            file_path=compiled_only.file_path,
            finding=Finding(
                kind=f.kind,
                message=f.message,
                sql_snippet=f.sql_snippet,
                line_start=line,
                line_end=line,
            ),
            source_span=SourceSpan(line, line, SpanBasis.COMPILED),
        )

    text = render_text(_report(structural=(_at(12),)))
    assert "L12 (compiled)  error  join_fanout" in text
    payload = json.loads(render_json(_report(structural=(_at(12),))))
    [f] = [f for f in payload["findings"] if f["family"] == "structural"]
    # compiled line preserved; source span mirrors it; basis flags the fallback.
    assert (f["line_start"], f["source_line_start"], f["line_basis"]) == (12, 12, "compiled")


def test_macro_call_finding_renders_via_macro_and_keeps_compiled_line() -> None:
    # A finding emitted inside a macro reports the `{{ ... }}` call line, marked
    # `(via macro)` so a reader knows it names the call site rather than the construct.
    # The compiled line the parser saw stays on `line_start` for anyone who needs it.
    finding = _structural(source_span=SourceSpan(3, 3, SpanBasis.MACRO_CALL))
    text = render_text(_report(structural=(finding,)))
    assert "L3 (via macro)  error  join_fanout" in text
    payload = json.loads(render_json(_report(structural=(finding,))))
    [f] = [f for f in payload["findings"] if f["family"] == "structural"]
    assert (f["line_start"], f["source_line_start"], f["line_basis"]) == (9, 3, "macro_call")


def test_unlocated_structural_finding_reports_null_source_span_and_basis() -> None:
    # A literal-only structural finding sqlglot stamped no line on (line 0) reports null
    # for the source span and basis, the same null contract the declaration family uses
    # for its unlocated findings. Both families agree on the no-line case.
    unlocated = LocatedFinding(
        model_unique_id=_MODEL,
        file_path="models/m.sql",
        finding=Finding(
            kind=FindingKind.JOIN_FANOUT,
            message="join can multiply rows",
            sql_snippet="",
            line_start=0,
            line_end=0,
        ),
        source_span=None,
    )
    payload = json.loads(render_json(_report(structural=(unlocated,))))
    [f] = [f for f in payload["findings"] if f["family"] == "structural"]
    assert f["source_line_start"] is None
    assert f["source_line_end"] is None
    assert f["line_basis"] is None


def _located_declaration(*, source_span: SourceSpan) -> CheckFinding:
    return CheckFinding(
        kind=CheckFindingKind.AGGREGATION_NOT_WELL_TYPED,
        message="reducing 'total' mixes a per-row companion held constant by nothing",
        model_unique_id=_MODEL,
        file_path="models/m.sql",
        column="total",
        line_start=7,
        line_end=7,
        source_span=source_span,
    )


def test_declaration_finding_back_maps_its_span_for_both_bases() -> None:
    # A located declaration finding back-maps its compiled span: the report points at the
    # source line and the JSON keeps the compiled span beside the back-mapped one. Both
    # bases ride the same declaration payload branch, so both are pinned here. (The
    # ``(compiled)`` text marker is the shared `_format_span` helper, pinned once on the
    # structural family.)
    mapped = _located_declaration(source_span=SourceSpan(3, 3, SpanBasis.SOURCE))
    assert "models/m.sql:L3" in render_text(_report(declaration=(mapped,)))
    payload = json.loads(render_json(_report(declaration=(mapped,))))
    [f] = [f for f in payload["findings"] if f["family"] == "declaration"]
    assert (f["line_start"], f["source_line_start"], f["line_basis"]) == (7, 3, "source")

    fallback = _located_declaration(source_span=SourceSpan(7, 7, SpanBasis.COMPILED))
    payload = json.loads(render_json(_report(declaration=(fallback,))))
    [f] = [f for f in payload["findings"] if f["family"] == "declaration"]
    assert (f["line_start"], f["source_line_start"], f["line_basis"]) == (7, 7, "compiled")


def test_unlocated_declaration_finding_reports_null_source_span() -> None:
    # A finding with no SQL site (a contract or coverage finding) stays unlocated: no
    # source span, no basis, the same nulls the compiled line fields already report.
    payload = json.loads(render_json(_report(declaration=(_declaration(),))))
    [f] = [f for f in payload["findings"] if f["family"] == "declaration"]
    assert f["line_start"] is None
    assert f["source_line_start"] is None
    assert f["line_basis"] is None


def test_json_suppression_payload_carries_directive_line_and_bare() -> None:
    # A -- noqa has no reason slot, so a suppression serializes as the directive line, the
    # bare flag, and whether the directive was read in the compiled frame.
    suppressed = (SuppressedFinding(located=_structural(), directive_line=8, bare=False),)
    payload = json.loads(render_json(_report(structural=(_structural(),), suppressed=suppressed)))
    [entry] = payload["suppressed"]
    assert entry["suppression"] == {
        "directive_line": 8,
        "bare": False,
        "directive_in_compiled": False,
    }


def test_json_suppression_payload_marks_compiled_frame_directive() -> None:
    # A macro body's `-- noqa` is recorded as a compiled-frame match so a JSON consumer can
    # read its line in compiled space rather than mislocating it to the source template.
    suppressed = (
        SuppressedFinding(
            located=_structural(), directive_line=5, bare=False, directive_in_compiled=True
        ),
    )
    payload = json.loads(render_json(_report(structural=(_structural(),), suppressed=suppressed)))
    [entry] = payload["suppressed"]
    assert entry["suppression"]["directive_in_compiled"] is True


# --- the noqa suppression hint -----------------------------------------------


def test_text_appends_suppression_hint_for_structural_findings() -> None:
    kind = FindingKind.JOIN_FANOUT
    text = render_text(_report(structural=(_structural(kind=kind),)))
    assert suppression_hint(kind) in text
    assert "-- noqa: DBLECT_JOIN_FANOUT" in text


def test_text_omits_hint_for_unlocated_declaration_findings() -> None:
    # A declaration finding with no line (the default `_declaration`) cannot carry a
    # directive, so the hint must not leak into its block.
    text = render_text(_report(declaration=(_declaration(),)))
    assert "noqa" not in text


def test_json_message_stays_the_pure_observation() -> None:
    # The hint is presentation: it rides the text reporter, never the JSON
    # `message`, which remains the detector's observation verbatim.
    observation = "ARRAY_AGG has no ORDER BY; element order across rows is undefined"
    structural = (_structural(message=observation, kind=FindingKind.UNORDERED_AGGREGATE),)
    payload = json.loads(render_json(_report(structural=structural)))
    [finding] = [f for f in payload["findings"] if f["family"] == "structural"]
    assert finding["message"] == observation
    assert "noqa" not in finding["message"]
