"""Render an :class:`~dblect.analysis.AnalysisReport` for a terminal or for CI.

One report, both detector families. The structural family keeps its by-model,
line-located rendering (click through to a file:line, with the offending snippet);
the declaration family keeps its model/column/contract rendering (where a line span
would not make sense). The two share one summary, one coverage block, and one JSON
schema, so a reader and a machine consumer each see every finding in one place rather
than reconciling two report shapes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from textwrap import indent
from typing import TypedDict, assert_never

from dblect.analysis import AnalysisFinding, AnalysisReport
from dblect.audit.walker import LocatedFinding, SkippedModel, SuppressedFinding
from dblect.check.findings import CheckFinding, SuppressedCheckFinding
from dblect.severity import severity_of
from dblect.sql import suppression_code, suppression_hint

# Both families and the coverage block live under one document; consumers branch on
# ``family`` per finding. Bumped to "2" when each finding gained a ``severity`` field.
JSON_SCHEMA_VERSION = "2"


def _partition_by_family(
    findings: Sequence[AnalysisFinding],
) -> tuple[list[LocatedFinding], list[CheckFinding]]:
    """Split ``findings`` into (structural, declaration), exhaustively. The ``match``
    is closed by ``assert_never`` so adding a third family is a typecheck error here,
    not a finding rendered in no block while still counted in the summary."""
    structural: list[LocatedFinding] = []
    declaration: list[CheckFinding] = []
    for finding in findings:
        match finding:
            case LocatedFinding():
                structural.append(finding)
            case CheckFinding():
                declaration.append(finding)
            case _:
                assert_never(finding)
    return structural, declaration


# --- text --------------------------------------------------------------------


def render_text(report: AnalysisReport) -> str:
    """Render ``report`` as plain text for a terminal."""
    structural, declaration = _partition_by_family(report.findings)

    sections: list[str] = [_summary_line(report), _coverage_block(report)]
    if report.check.load_issues:
        lines = [f"  could not load {i.module}: {i.message}" for i in report.check.load_issues]
        sections.append("load issues:\n" + "\n".join(lines))
    if structural:
        sections.append("structural findings:\n" + _structural_block(structural))
    if declaration:
        sections.append(
            "declaration findings:\n" + "\n\n".join(_declaration_block(f) for f in declaration)
        )
    if report.audit.suppressed or report.check.suppressed:
        sections.append(_suppressed_block(report.audit.suppressed, report.check.suppressed))
    if report.audit.skipped:
        sections.append(_skipped_block(report.audit.skipped))
    if report.check.unbuilt:
        # Surfaced, not silent: a model the analysis could not read is a coverage
        # gap, so any finding it would have carried is simply absent. The reader
        # needs to know that rather than read an empty report as "all clear".
        lines = [f"  {m.unique_id}: {m.reason}" for m in report.check.unbuilt]
        sections.append("could not analyze (no findings reported for these):\n" + "\n".join(lines))
    return "\n\n".join(sections) + "\n"


def _summary_line(report: AnalysisReport) -> str:
    n = len(report.findings)
    word = "finding" if n == 1 else "findings"
    return (
        f"dblect: {n} {word} over {report.check.models_analyzed} models "
        f"({report.check.contracts_resolved} contracts resolved, "
        f"{report.audit.models_scanned} scanned, "
        f"{report.check.predicates_collected} predicate(s) collected)"
    )


def _coverage_block(report: AnalysisReport) -> str:
    """The check family's coverage metrics, so thin coverage cannot hide behind a
    short finding list. Resolution is the lineage the propagator could follow;
    grounding is, among that, how many columns a fact actually checks."""
    res = report.check.resolution
    frac = res.fraction
    res_pct = "n/a" if frac is None else f"{frac:.1%}"
    star = f"; {res.unexpanded_stars} unexpanded SELECT *" if res.unexpanded_stars else ""
    lines = [
        "coverage:",
        f"  resolution: {res_pct} of columns ({res.resolved_columns}/{res.sites}){star}",
    ]
    g = report.check.grounding
    per_prop = "; ".join(f"{p.property_name} {p.grounded}/{p.resolved}" for p in g.by_property)
    if per_prop:
        lines.append(f"  grounding: {per_prop}")
    lines.append(
        f"  contract columns checkable: {g.contract_columns_checkable}/{g.contract_columns}"
    )
    lines.append(_worlds_line(report))
    return "\n".join(lines)


def _worlds_line(report: AnalysisReport) -> str:
    """How many worlds the analysis checked. The single-world case says so plainly so
    a clean report is not read as covering every configuration."""
    w = report.check.worlds
    axes = ", ".join(w.axes_enumerated)
    if w.worlds_enumerated <= 1 and not axes:
        return "  worlds: 1 (base); no flag axes enumerated"
    over = f" over axes: {axes}" if axes else ""
    return f"  worlds: {w.worlds_enumerated} enumerated{over}"


def _structural_block(findings: Sequence[LocatedFinding]) -> str:
    by_model: dict[str, list[LocatedFinding]] = defaultdict(list)
    for lf in findings:
        by_model[lf.model_unique_id].append(lf)
    chunks: list[str] = []
    for uid in sorted(by_model):
        group = sorted(by_model[uid], key=lambda lf: (lf.finding.line_start, lf.finding.line_end))
        # Every finding in a model shares the model's file path; take it from the first.
        path = group[0].file_path or uid
        chunks.append(f"  {path}  ({uid})")
        chunks.extend(indent(_render_structural(lf), "    ") for lf in group)
    return "\n".join(chunks)


def _render_structural(lf: LocatedFinding) -> str:
    loc = _format_line_range(lf.finding.line_start, lf.finding.line_end)
    head = f"{loc}  {severity_of(lf).value}  {lf.finding.kind.value}"
    body_lines: list[str] = [head]
    body_lines.extend(indent(line, "    ") for line in lf.finding.message.splitlines() or [""])
    # The suppression nudge is presentation, not observation, so it lives here rather
    # than in `Finding.message` (the JSON `message` stays the pure observation). Every
    # structural finding is line-suppressible, so the hint rides all of them.
    body_lines.append(indent(suppression_hint(lf.finding.kind), "    "))
    snippet = lf.finding.sql_snippet.strip()
    if snippet:
        body_lines.append(indent(f"snippet: {snippet}", "    "))
    return "\n".join(body_lines)


def _declaration_block(finding: CheckFinding) -> str:
    where = finding.model_unique_id or finding.contract or "<project>"
    kind = finding.kind.value
    # A contract issue names its specific cause inline, so the reader distinguishes an
    # unsourced field from any other contract issue without reading the body.
    if finding.code is not None:
        kind += f" ({finding.code.value})"
    head = f"  {severity_of(finding).value}  {kind}  {where}"
    if finding.column:
        head += f".{finding.column}"
    lines = [head, f"      {finding.message}"]
    if finding.file_path:
        loc = (
            f":{_format_line_range(finding.line_start, finding.line_end)}"
            if finding.line_start
            else ""
        )
        lines.append(f"      {finding.file_path}{loc}")
    # A finding pinned to a line is line-suppressible, so it carries the same nudge the
    # structural family does. One that could not be located (line 0: a contract or
    # coverage finding) has no line to put a directive on, so the hint is omitted.
    if finding.line_start:
        lines.append(f"      {suppression_hint(finding.kind)}")
    return "\n".join(lines)


def _format_line_range(start: int, end: int) -> str:
    if start == 0:
        return "L?"
    if start == end:
        return f"L{start}"
    return f"L{start}-{end}"


def _suppressed_block(
    structural: Sequence[SuppressedFinding],
    declaration: Sequence[SuppressedCheckFinding],
) -> str:
    """Both families' silenced findings under one heading, so a reviewer reads every
    recorded acknowledgement in one place. Each line names where the finding sat, its
    kind, how it was silenced (bare ``noqa`` or the specific ``noqa: DBLECT_<KIND>``),
    and the line the directive sat on."""
    lines = ["suppressed:"]
    rows: list[tuple[str, int, int, str, str, int]] = []
    for s in structural:
        f = s.located.finding
        path = s.located.file_path or s.located.model_unique_id
        via = "noqa" if s.bare else f"noqa: {suppression_code(f.kind)}"
        rows.append((path, f.line_start, f.line_end, f.kind.value, via, s.directive_line))
    for c in declaration:
        cf = c.finding
        path = cf.file_path or cf.model_unique_id or "<project>"
        via = "noqa" if c.bare else f"noqa: {suppression_code(cf.kind)}"
        rows.append((path, cf.line_start, cf.line_end, cf.kind.value, via, c.directive_line))
    for path, line_start, line_end, kind, via, directive_line in sorted(rows):
        loc = _format_line_range(line_start, line_end)
        lines.append(f"  {path}:{loc}  {kind}  suppressed by {via} @ L{directive_line}")
    return "\n".join(lines)


def _skipped_block(skipped: Iterable[SkippedModel]) -> str:
    lines = ["skipped:"]
    lines.extend(
        f"  {s.unique_id}  ({s.reason})" for s in sorted(skipped, key=lambda x: x.unique_id)
    )
    return "\n".join(lines)


# --- json --------------------------------------------------------------------


class JsonSummary(TypedDict):
    findings: int
    structural: int
    declaration: int
    models_analyzed: int
    models_scanned: int
    contracts_resolved: int
    predicates_collected: int
    suppressed: int
    skipped: int
    load_issues: int
    unbuilt: int


class JsonPropertyGrounding(TypedDict):
    property: str
    grounded: int
    resolved: int


class JsonResolutionCoverage(TypedDict):
    resolved_columns: int
    blind_columns: int
    sites: int
    unexpanded_stars: int
    fraction: float | None


class JsonGroundingCoverage(TypedDict):
    by_property: list[JsonPropertyGrounding]
    contract_columns: int
    contract_columns_checkable: int


class JsonWorldCoverage(TypedDict):
    worlds_enumerated: int
    axes_enumerated: list[str]


class JsonCoverage(TypedDict):
    resolution: JsonResolutionCoverage
    grounding: JsonGroundingCoverage
    worlds: JsonWorldCoverage


class JsonFinding(TypedDict):
    family: str
    kind: str
    severity: str
    message: str
    model_unique_id: str | None
    file_path: str | None
    column: str | None
    contract: str | None
    line_start: int | None
    line_end: int | None
    sql_snippet: str | None


class JsonSuppression(TypedDict):
    directive_line: int
    bare: bool


class JsonSuppressedFinding(JsonFinding):
    suppression: JsonSuppression


class JsonSkipped(TypedDict):
    unique_id: str
    reason: str


class JsonLoadIssue(TypedDict):
    module: str
    message: str


class JsonUnbuilt(TypedDict):
    unique_id: str
    reason: str


class JsonReport(TypedDict):
    schema_version: str
    summary: JsonSummary
    coverage: JsonCoverage
    findings: list[JsonFinding]
    suppressed: list[JsonSuppressedFinding]
    skipped: list[JsonSkipped]
    load_issues: list[JsonLoadIssue]
    unbuilt: list[JsonUnbuilt]


def render_json(report: AnalysisReport, *, indent_spaces: int = 2) -> str:
    """Render ``report`` as a stable JSON document. Each finding carries a ``family``
    discriminator (``structural`` or ``declaration``); the locator fields not relevant
    to a family are ``null``."""
    structural, declaration = _partition_by_family(report.findings)
    res, g, w = report.check.resolution, report.check.grounding, report.check.worlds
    payload: JsonReport = {
        "schema_version": JSON_SCHEMA_VERSION,
        "summary": {
            "findings": len(report.findings),
            "structural": len(structural),
            "declaration": len(declaration),
            "models_analyzed": report.check.models_analyzed,
            "models_scanned": report.audit.models_scanned,
            "contracts_resolved": report.check.contracts_resolved,
            "predicates_collected": report.check.predicates_collected,
            "suppressed": len(report.audit.suppressed) + len(report.check.suppressed),
            "skipped": len(report.audit.skipped),
            "load_issues": len(report.check.load_issues),
            "unbuilt": len(report.check.unbuilt),
        },
        "coverage": {
            "resolution": {
                "resolved_columns": res.resolved_columns,
                "blind_columns": res.blind_columns,
                "sites": res.sites,
                "unexpanded_stars": res.unexpanded_stars,
                "fraction": res.fraction,
            },
            "grounding": {
                "by_property": [
                    {"property": p.property_name, "grounded": p.grounded, "resolved": p.resolved}
                    for p in g.by_property
                ],
                "contract_columns": g.contract_columns,
                "contract_columns_checkable": g.contract_columns_checkable,
            },
            "worlds": {
                "worlds_enumerated": w.worlds_enumerated,
                "axes_enumerated": list(w.axes_enumerated),
            },
        },
        "findings": [_finding_payload(f) for f in report.findings],
        "suppressed": [
            *(_suppressed_payload(s) for s in report.audit.suppressed),
            *(_suppressed_check_payload(c) for c in report.check.suppressed),
        ],
        "skipped": [{"unique_id": s.unique_id, "reason": s.reason} for s in report.audit.skipped],
        "load_issues": [
            {"module": i.module, "message": i.message} for i in report.check.load_issues
        ],
        "unbuilt": [{"unique_id": m.unique_id, "reason": m.reason} for m in report.check.unbuilt],
    }
    return json.dumps(payload, indent=indent_spaces, sort_keys=True)


def _finding_payload(finding: AnalysisFinding) -> JsonFinding:
    match finding:
        case CheckFinding():
            # A located finding (a domain-type or aggregation finding pinned to its
            # projection) carries its line span so a consumer can point at it and
            # acknowledge it; an unlocated one (line 0) reports null, as before.
            located = finding.line_start > 0
            return {
                "family": "declaration",
                "kind": finding.kind.value,
                "severity": severity_of(finding).value,
                "message": finding.message,
                "model_unique_id": finding.model_unique_id,
                "file_path": finding.file_path,
                "column": finding.column,
                "contract": finding.contract,
                "line_start": finding.line_start if located else None,
                "line_end": finding.line_end if located else None,
                "sql_snippet": None,
            }
        case LocatedFinding():
            inner = finding.finding
            return {
                "family": "structural",
                "kind": inner.kind.value,
                "severity": severity_of(finding).value,
                "message": inner.message,
                "model_unique_id": finding.model_unique_id,
                "file_path": finding.file_path,
                "column": None,
                "contract": None,
                "line_start": inner.line_start,
                "line_end": inner.line_end,
                "sql_snippet": inner.sql_snippet,
            }
    assert_never(finding)


def _suppressed_payload(s: SuppressedFinding) -> JsonSuppressedFinding:
    base = _finding_payload(s.located)
    return {**base, "suppression": {"directive_line": s.directive_line, "bare": s.bare}}


def _suppressed_check_payload(c: SuppressedCheckFinding) -> JsonSuppressedFinding:
    base = _finding_payload(c.finding)
    return {**base, "suppression": {"directive_line": c.directive_line, "bare": c.bare}}
