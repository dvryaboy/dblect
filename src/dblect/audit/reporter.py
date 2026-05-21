"""Render an ``AuditReport`` for human consumption.

The text format groups findings by their originating model so a developer
scanning the output can click through to a file:line and see every issue
the file accumulated in one place. Suppressed findings show up in a compact
trailing block so PR reviewers can audit what was muted and why.

Machine-readable formats (JSON, eventually SARIF) live elsewhere; this module
is for what gets printed on a terminal.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from textwrap import indent

from dblect.audit.walker import AuditReport, LocatedFinding, SkippedModel, SuppressedFinding


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
            loc = _format_line_range(
                s.located.finding.line_start, s.located.finding.line_end
            )
            lines.append(
                f"  {path}:{loc}  {s.located.finding.kind.value}  -- {s.reason}"
            )
    return "\n".join(lines)


def _skipped_block(skipped: Iterable[SkippedModel]) -> str:
    lines = ["skipped:"]
    lines.extend(
        f"  {s.unique_id}  ({s.reason})"
        for s in sorted(skipped, key=lambda x: x.unique_id)
    )
    return "\n".join(lines)
