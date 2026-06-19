"""Render an :class:`~dblect.analysis.AnalysisReport` as a SARIF 2.1.0 log.

SARIF is the format GitHub code scanning, SonarQube, and similar surfaces ingest to
render findings as pull-request annotations. The mapping: each finding is a ``result``
keyed by a ``<family>/<kind>`` rule id; structural findings get a file location and a
line region, declaration findings a logical location on their model/column/contract;
suppressed findings keep their justification; and models the analysis could not read
become notifications. The schema is OASIS SARIF 2.1.0
(https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html).
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypedDict, assert_never

from dblect.analysis import AnalysisFinding, AnalysisReport, cross_world_identity
from dblect.audit.walker import LocatedFinding, SkippedModel, SuppressedFinding
from dblect.check.findings import CheckFinding, UnbuiltModel
from dblect.loader import LoadIssue

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
_INFORMATION_URI = "https://github.com/dvryaboy/dblect"

# Per-kind severity is future work; until it lands every finding reports at one level.
_DEFAULT_LEVEL: Literal["warning"] = "warning"
_FINGERPRINT_KEY = "dblectFindingIdentity/v1"

_Level = Literal["none", "note", "warning", "error"]
_Family = Literal["structural", "declaration"]


# --- SARIF object shapes (the subset dblect emits) ---------------------------


class _Message(TypedDict):
    text: str


class _Configuration(TypedDict):
    level: _Level


class _ReportingDescriptor(TypedDict):
    id: str
    name: str
    shortDescription: _Message
    defaultConfiguration: _Configuration


class _ToolComponent(TypedDict):
    name: str
    version: str
    informationUri: str
    rules: list[_ReportingDescriptor]


class _Tool(TypedDict):
    driver: _ToolComponent


class _ArtifactLocation(TypedDict):
    uri: str


class _Region(TypedDict):
    startLine: int
    endLine: int


class _PhysicalLocation(TypedDict, total=False):
    artifactLocation: _ArtifactLocation
    region: _Region


class _LogicalLocation(TypedDict):
    fullyQualifiedName: str


class _Location(TypedDict, total=False):
    physicalLocation: _PhysicalLocation
    logicalLocations: list[_LogicalLocation]


class _Suppression(TypedDict):
    kind: Literal["inSource"]
    justification: str


class _Result(TypedDict, total=False):
    ruleId: str
    ruleIndex: int
    level: _Level
    message: _Message
    locations: list[_Location]
    suppressions: list[_Suppression]
    partialFingerprints: dict[str, str]


class _DescriptorReference(TypedDict):
    id: str


class _Notification(TypedDict, total=False):
    descriptor: _DescriptorReference
    level: _Level
    message: _Message
    locations: list[_Location]


class _Invocation(TypedDict):
    executionSuccessful: bool
    toolExecutionNotifications: list[_Notification]


class _Run(TypedDict):
    tool: _Tool
    results: list[_Result]
    invocations: list[_Invocation]


# ``$schema`` is not an identifier, so the top-level log uses the functional syntax.
_SarifLog = TypedDict("_SarifLog", {"$schema": str, "version": str, "runs": list[_Run]})


# --- rendering ---------------------------------------------------------------


def render_sarif(report: AnalysisReport, *, version: str, indent_spaces: int = 2) -> str:
    """Render ``report`` as a SARIF 2.1.0 log. ``version`` stamps the tool driver."""
    active = list(report.findings)
    suppressed = list(report.audit.suppressed)

    rules, rule_index = _build_rules(active, suppressed)
    results: list[_Result] = [_result_for(f, rule_index) for f in active]
    results.extend(_suppressed_result(s, rule_index) for s in suppressed)

    driver: _ToolComponent = {
        "name": "dblect",
        "version": version,
        "informationUri": _INFORMATION_URI,
        "rules": rules,
    }
    run: _Run = {
        "tool": {"driver": driver},
        "results": results,
        # A skipped model is a coverage gap surfaced as a notification, not a failure
        # of the run itself, so the invocation still reports success.
        "invocations": [
            {
                "executionSuccessful": True,
                "toolExecutionNotifications": _notifications(
                    skipped=report.audit.skipped,
                    unbuilt=report.check.unbuilt,
                    load_issues=report.check.load_issues,
                ),
            }
        ],
    }
    document: _SarifLog = {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [run],
    }
    return json.dumps(document, indent=indent_spaces, sort_keys=True)


def _build_rules(
    active: list[AnalysisFinding], suppressed: list[SuppressedFinding]
) -> tuple[list[_ReportingDescriptor], dict[str, int]]:
    """The rules every result references, sorted by id, with an id->index map built
    from that order so each result's ``ruleIndex`` is correct."""
    ids = {_rule_id(f) for f in active}
    ids.update(_rule_id(s.located) for s in suppressed)
    rules = [_descriptor(rule_id) for rule_id in sorted(ids)]
    return rules, {rule["id"]: i for i, rule in enumerate(rules)}


def _descriptor(rule_id: str) -> _ReportingDescriptor:
    _, _, kind = rule_id.partition("/")
    return {
        "id": rule_id,
        "name": _pascal_case(kind),
        "shortDescription": {"text": kind.replace("_", " ")},
        "defaultConfiguration": {"level": _DEFAULT_LEVEL},
    }


def _result_for(finding: AnalysisFinding, rule_index: dict[str, int]) -> _Result:
    rule_id = _rule_id(finding)
    result: _Result = {
        "ruleId": rule_id,
        "ruleIndex": rule_index[rule_id],
        "level": _DEFAULT_LEVEL,
        "message": {"text": _message(finding)},
        "partialFingerprints": {_FINGERPRINT_KEY: _fingerprint(finding)},
    }
    locations = _locations(finding)
    if locations:
        result["locations"] = locations
    return result


def _suppressed_result(s: SuppressedFinding, rule_index: dict[str, int]) -> _Result:
    # A suppressed finding is still a result, so a surface can show what was triaged
    # away rather than silently dropping it.
    result = _result_for(s.located, rule_index)
    result["suppressions"] = [{"kind": "inSource", "justification": s.reason}]
    return result


def _rule_id(finding: AnalysisFinding) -> str:
    # Namespacing by family makes a rule id identify one rule by construction, rather
    # than relying on the two kind enums never sharing a value.
    family, kind = _family_and_kind(finding)
    return f"{family}/{kind}"


def _family_and_kind(finding: AnalysisFinding) -> tuple[_Family, str]:
    match finding:
        case CheckFinding():
            return "declaration", finding.kind.value
        case LocatedFinding():
            return "structural", finding.finding.kind.value
    assert_never(finding)


def _message(finding: AnalysisFinding) -> str:
    match finding:
        case CheckFinding():
            return finding.message
        case LocatedFinding():
            return finding.finding.message
    assert_never(finding)


def _locations(finding: AnalysisFinding) -> list[_Location]:
    match finding:
        case CheckFinding():
            return _declaration_locations(finding)
        case LocatedFinding():
            return _structural_locations(finding)
    assert_never(finding)


def _structural_locations(finding: LocatedFinding) -> list[_Location]:
    location: _Location = {"logicalLocations": [{"fullyQualifiedName": finding.model_unique_id}]}
    if finding.file_path is not None:
        physical: _PhysicalLocation = {"artifactLocation": {"uri": finding.file_path}}
        region = _region(finding.finding.line_start, finding.finding.line_end)
        if region is not None:
            physical["region"] = region
        location["physicalLocation"] = physical
    return [location]


def _declaration_locations(finding: CheckFinding) -> list[_Location]:
    # No line span: a declaration finding locates by logical name, with a physical
    # location only when the source file is known. A project-wide finding anchors to
    # neither and yields no location, which SARIF permits.
    name = _declaration_name(finding)
    location: _Location = {}
    if name is not None:
        location["logicalLocations"] = [{"fullyQualifiedName": name}]
    if finding.file_path is not None:
        location["physicalLocation"] = {"artifactLocation": {"uri": finding.file_path}}
    return [location] if location else []


def _declaration_name(finding: CheckFinding) -> str | None:
    if finding.model_unique_id is None:
        return finding.contract
    if finding.column is not None:
        return f"{finding.model_unique_id}.{finding.column}"
    return finding.model_unique_id


def _region(line_start: int, line_end: int) -> _Region | None:
    # A SARIF region needs startLine >= 1; line 0 is the detector's "no line" sentinel.
    if line_start < 1:
        return None
    return {"startLine": line_start, "endLine": max(line_start, line_end)}


def _notifications(
    *,
    skipped: tuple[SkippedModel, ...],
    unbuilt: tuple[UnbuiltModel, ...],
    load_issues: tuple[LoadIssue, ...],
) -> list[_Notification]:
    # What the analysis could not read, so an absent finding is not read as a clean one.
    return [
        *(
            _notification(
                "model_skipped", f"model not scanned: {s.unique_id} ({s.reason})", s.unique_id
            )
            for s in skipped
        ),
        *(
            _notification(
                "model_unbuilt", f"model not analyzed: {m.unique_id} ({m.reason})", m.unique_id
            )
            for m in unbuilt
        ),
        *(
            _notification(
                "declaration_load_issue", f"could not load {i.module}: {i.message}", i.module
            )
            for i in load_issues
        ),
    ]


def _notification(descriptor_id: str, text: str, logical_name: str) -> _Notification:
    return {
        "descriptor": {"id": descriptor_id},
        "level": _DEFAULT_LEVEL,
        "message": {"text": text},
        "locations": [{"logicalLocations": [{"fullyQualifiedName": logical_name}]}],
    }


def _fingerprint(finding: AnalysisFinding) -> str:
    # Hash the cross-run identity (kind plus where it lands, no line numbers) so a
    # consumer can track the same finding across compilations.
    return hashlib.sha256(repr(cross_world_identity(finding)).encode("utf-8")).hexdigest()


def _pascal_case(kind: str) -> str:
    return "".join(part.capitalize() for part in kind.split("_"))
