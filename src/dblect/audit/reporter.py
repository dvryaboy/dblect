"""Render an ``AuditReport`` for human or machine consumption.

The text format groups findings by their originating model so a developer
scanning the output can click through to a file:line and see every issue
the file accumulated in one place. Suppressed findings show up in a compact
trailing block so PR reviewers can audit what was muted and why.

The JSON format is a stable, documented schema for CI and editor
integrations. It includes a ``schema_version`` field so consumers can
detect breaking changes; we'll bump it any time the shape changes
incompatibly. The shape is reflected in the `JsonReport` TypedDict below.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from textwrap import indent
from typing import TypedDict

from dblect.audit.walker import AuditReport, LocatedFinding, SkippedModel, SuppressedFinding

JSON_SCHEMA_VERSION = "1"


class JsonSummary(TypedDict):
    models_scanned: int
    findings: int
    suppressed: int
    skipped: int


class JsonFinding(TypedDict):
    model_unique_id: str
    file_path: str | None
    kind: str
    line_start: int
    line_end: int
    message: str
    sql_snippet: str


class JsonSuppression(TypedDict):
    reason: str
    directive_line: int


class JsonSuppressedFinding(JsonFinding):
    suppression: JsonSuppression


class JsonSkipped(TypedDict):
    unique_id: str
    reason: str


class JsonReport(TypedDict):
    schema_version: str
    summary: JsonSummary
    findings: list[JsonFinding]
    suppressed: list[JsonSuppressedFinding]
    skipped: list[JsonSkipped]


def render_text(report: AuditReport) -> str:
    """Render `report` as plain text suitable for a terminal."""
    sections: list[str] = []
    sections.append(_summary_line(report))
    if report.findings:
        sections.append(_findings_block(report.findings))
    if report.suppressed:
        sections.append(_suppressed_block(report.suppressed))
    if report.skipped:
        sections.append(_skipped_block(report.skipped))
    return "\n\n".join(sections) + "\n"


def _summary_line(report: AuditReport) -> str:
    n_findings = len(report.findings)
    finding_word = "finding" if n_findings == 1 else "findings"
    parts = [f"scanned {report.models_scanned} models", f"{n_findings} {finding_word}"]
    if report.suppressed:
        parts.append(f"{len(report.suppressed)} suppressed")
    if report.skipped:
        parts.append(f"{len(report.skipped)} skipped")
    return "audit: " + ", ".join(parts)


def _findings_block(findings: Sequence[LocatedFinding]) -> str:
    by_model: dict[str, list[LocatedFinding]] = defaultdict(list)
    for lf in findings:
        by_model[lf.model_unique_id].append(lf)
    chunks: list[str] = []
    for uid in sorted(by_model):
        group = sorted(by_model[uid], key=lambda lf: (lf.finding.line_start, lf.finding.line_end))
        # Take file path from the first finding in the group; every finding in
        # a given model has the same path.
        path = group[0].file_path or uid
        chunks.append(f"{path}  ({uid})")
        chunks.extend(indent(_render_finding(lf), "  ") for lf in group)
    return "\n".join(chunks)


def _render_finding(lf: LocatedFinding) -> str:
    location = _format_line_range(lf.finding.line_start, lf.finding.line_end)
    head = f"{location}  {lf.finding.kind.value}"
    body_lines: list[str] = [head]
    body_lines.extend(indent(line, "    ") for line in lf.finding.message.splitlines() or [""])
    snippet = lf.finding.sql_snippet.strip()
    if snippet:
        body_lines.append(indent(f"snippet: {snippet}", "    "))
    return "\n".join(body_lines)


def _format_line_range(start: int, end: int) -> str:
    if start == 0:
        return "L?"
    if start == end:
        return f"L{start}"
    return f"L{start}-{end}"


def _suppressed_block(suppressed: Sequence[SuppressedFinding]) -> str:
    lines = ["suppressed:"]
    by_model: dict[str, list[SuppressedFinding]] = defaultdict(list)
    for s in suppressed:
        by_model[s.located.model_unique_id].append(s)
    for uid in sorted(by_model):
        for s in sorted(by_model[uid], key=lambda x: x.located.finding.line_start):
            path = s.located.file_path or uid
            loc = _format_line_range(s.located.finding.line_start, s.located.finding.line_end)
            lines.append(f"  {path}:{loc}  {s.located.finding.kind.value}  -- {s.reason}")
    return "\n".join(lines)


def _skipped_block(skipped: Iterable[SkippedModel]) -> str:
    lines = ["skipped:"]
    lines.extend(
        f"  {s.unique_id}  ({s.reason})" for s in sorted(skipped, key=lambda x: x.unique_id)
    )
    return "\n".join(lines)


def render_json(report: AuditReport, *, indent_spaces: int = 2) -> str:
    """Render `report` as a stable JSON document.

    The schema is documented at the module level via ``JSON_SCHEMA_VERSION``
    and reflected in `JsonReport`. Consumers should branch on the version
    field; we'll bump it on any incompatible change to the shape.
    """
    payload: JsonReport = {
        "schema_version": JSON_SCHEMA_VERSION,
        "summary": {
            "models_scanned": report.models_scanned,
            "findings": len(report.findings),
            "suppressed": len(report.suppressed),
            "skipped": len(report.skipped),
        },
        "findings": [_finding_payload(lf) for lf in report.findings],
        "suppressed": [_suppressed_payload(s) for s in report.suppressed],
        "skipped": [JsonSkipped(unique_id=s.unique_id, reason=s.reason) for s in report.skipped],
    }
    return json.dumps(payload, indent=indent_spaces, sort_keys=True)


def _finding_payload(lf: LocatedFinding) -> JsonFinding:
    return {
        "model_unique_id": lf.model_unique_id,
        "file_path": lf.file_path,
        "kind": lf.finding.kind.value,
        "line_start": lf.finding.line_start,
        "line_end": lf.finding.line_end,
        "message": lf.finding.message,
        "sql_snippet": lf.finding.sql_snippet,
    }


def _suppressed_payload(s: SuppressedFinding) -> JsonSuppressedFinding:
    base = _finding_payload(s.located)
    return {
        **base,
        "suppression": {
            "reason": s.reason,
            "directive_line": s.directive_line,
        },
    }
