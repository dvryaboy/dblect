"""Render a ``CheckReport`` for a terminal or for CI / editors.

The text format lists each finding with the model it lands on, so a reviewer can
click through. The JSON format is a stable, versioned schema for machine
consumers, mirroring the audit reporter's shape so a downstream tool can read both
report kinds the same way.
"""

from __future__ import annotations

import json
from typing import TypedDict

from dblect.check.findings import CheckFinding, CheckReport

# Bumped to 2 with the coverage block (resolution + grounding) added to the JSON.
JSON_SCHEMA_VERSION = "2"


class JsonSummary(TypedDict):
    contracts_resolved: int
    models_propagated: int
    models_analyzed: int
    predicates_collected: int
    findings: int
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


class JsonCoverage(TypedDict):
    resolution: JsonResolutionCoverage
    grounding: JsonGroundingCoverage


class JsonFinding(TypedDict):
    kind: str
    message: str
    model_unique_id: str | None
    file_path: str | None
    column: str | None
    contract: str | None


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
    load_issues: list[JsonLoadIssue]
    unbuilt: list[JsonUnbuilt]


def render_text(report: CheckReport) -> str:
    sections: list[str] = [_summary_line(report), _coverage_block(report)]
    if report.load_issues:
        lines = [
            f"  could not load {issue.module}: {issue.message}" for issue in report.load_issues
        ]
        sections.append("load issues:\n" + "\n".join(lines))
    if report.findings:
        sections.append("\n\n".join(_finding_block(f) for f in report.findings))
    if report.unbuilt:
        # Surfaced, not silent: a model the analysis could not read is a coverage
        # gap, so any finding it would have carried is simply absent. The reader
        # needs to know that rather than read an empty report as "all clear".
        lines = [f"  {m.unique_id}: {m.reason}" for m in report.unbuilt]
        sections.append("could not analyze (no findings reported for these):\n" + "\n".join(lines))
    return "\n\n".join(sections) + "\n"


def _coverage_block(report: CheckReport) -> str:
    """The two coverage metrics, rendered so thin coverage cannot hide behind a
    short finding list. Resolution is the lineage the propagator could follow;
    grounding is, among that, how many columns a fact actually checks."""
    res = report.resolution
    frac = res.fraction
    res_pct = "n/a" if frac is None else f"{frac:.1%}"
    star = f"; {res.unexpanded_stars} unexpanded SELECT *" if res.unexpanded_stars else ""
    lines = [
        "coverage:",
        f"  resolution: {res_pct} of columns ({res.resolved_columns}/{res.sites}){star}",
    ]
    g = report.grounding
    per_prop = "; ".join(f"{p.property_name} {p.grounded}/{p.resolved}" for p in g.by_property)
    if per_prop:
        lines.append(f"  grounding: {per_prop}")
    lines.append(
        f"  contract columns checkable: {g.contract_columns_checkable}/{g.contract_columns}"
    )
    return "\n".join(lines)


def _summary_line(report: CheckReport) -> str:
    n = len(report.findings)
    word = "finding" if n == 1 else "findings"
    unbuilt = f"; {len(report.unbuilt)} model(s) could not be analyzed" if report.unbuilt else ""
    return (
        f"checked {report.contracts_resolved} contracts over "
        f"{report.models_analyzed} models: {n} {word}"
        f" ({report.predicates_collected} predicate(s) collected; run requires materialized data)"
        f"{unbuilt}"
    )


def _finding_block(finding: CheckFinding) -> str:
    where = finding.model_unique_id or finding.contract or "<project>"
    head = f"{finding.kind.value}  {where}"
    if finding.column:
        head += f".{finding.column}"
    lines = [head, f"      {finding.message}"]
    if finding.file_path:
        lines.append(f"      {finding.file_path}")
    return "\n".join(lines)


def render_json(report: CheckReport) -> str:
    payload: JsonReport = {
        "schema_version": JSON_SCHEMA_VERSION,
        "summary": {
            "contracts_resolved": report.contracts_resolved,
            "models_propagated": report.models_propagated,
            "models_analyzed": report.models_analyzed,
            "predicates_collected": report.predicates_collected,
            "findings": len(report.findings),
            "load_issues": len(report.load_issues),
            "unbuilt": len(report.unbuilt),
        },
        "coverage": {
            "resolution": {
                "resolved_columns": report.resolution.resolved_columns,
                "blind_columns": report.resolution.blind_columns,
                "sites": report.resolution.sites,
                "unexpanded_stars": report.resolution.unexpanded_stars,
                "fraction": report.resolution.fraction,
            },
            "grounding": {
                "by_property": [
                    {"property": p.property_name, "grounded": p.grounded, "resolved": p.resolved}
                    for p in report.grounding.by_property
                ],
                "contract_columns": report.grounding.contract_columns,
                "contract_columns_checkable": report.grounding.contract_columns_checkable,
            },
        },
        "findings": [
            {
                "kind": f.kind.value,
                "message": f.message,
                "model_unique_id": f.model_unique_id,
                "file_path": f.file_path,
                "column": f.column,
                "contract": f.contract,
            }
            for f in report.findings
        ],
        "load_issues": [{"module": i.module, "message": i.message} for i in report.load_issues],
        "unbuilt": [{"unique_id": m.unique_id, "reason": m.reason} for m in report.unbuilt],
    }
    return json.dumps(payload, indent=2)
