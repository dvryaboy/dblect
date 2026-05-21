"""Run SQL detectors over every model in a manifest, return a typed report.

The walker is the substrate the CLI sits on. It iterates ``manifest.models``,
parses each model's ``raw_code`` through the Jinja-redacting SQL parser, runs
the configured detectors, and attaches each finding to its originating model
and source file path.

Models whose ``raw_code`` is absent (sources, seeds, packages that didn't
expose SQL) and models whose SQL fails to parse are recorded on the report as
skipped, with a reason. The walker never raises on per-model failure: one bad
model shouldn't blind the audit to the rest of the project.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from dblect.audit.suppress import apply, parse_directives
from dblect.manifest import Manifest, Node
from dblect.sql import (
    Finding,
    FindingKind,
    ParsedSQL,
    SQLParseError,
    detect_coalesce_on_join_key,
    detect_non_deterministic_function,
    detect_null_group_after_outer_join,
    detect_unordered_aggregate,
    detect_unordered_window,
    detect_where_on_outer_joined_nullable,
)

Detector = Callable[[ParsedSQL], tuple[Finding, ...]]

DEFAULT_DETECTORS: tuple[Detector, ...] = (
    detect_null_group_after_outer_join,
    detect_coalesce_on_join_key,
    detect_unordered_window,
    detect_unordered_aggregate,
    detect_where_on_outer_joined_nullable,
    detect_non_deterministic_function,
)


@dataclass(frozen=True, slots=True)
class LocatedFinding:
    """A `Finding` plus the model and file it came from."""

    model_unique_id: str
    file_path: str | None
    finding: Finding


@dataclass(frozen=True, slots=True)
class SuppressedFinding:
    """A finding that a ``-- noqa-fixture:`` directive silenced, with its reason."""

    located: LocatedFinding
    reason: str
    directive_line: int


@dataclass(frozen=True, slots=True)
class SkippedModel:
    """A model the walker couldn't scan, with the reason."""

    unique_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class AuditReport:
    """The output of one ``run_audit`` invocation."""

    findings: tuple[LocatedFinding, ...]
    suppressed: tuple[SuppressedFinding, ...]
    skipped: tuple[SkippedModel, ...]
    models_scanned: int

    @property
    def counts_by_kind(self) -> Mapping[FindingKind, int]:
        c: Counter[FindingKind] = Counter(lf.finding.kind for lf in self.findings)
        return dict(c)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)


def run_audit(
    manifest: Manifest,
    *,
    detectors: Sequence[Detector] = DEFAULT_DETECTORS,
    dialect: str | None = "duckdb",
) -> AuditReport:
    """Run `detectors` over every model in `manifest`.

    Sources, seeds, and snapshots are not scanned: they have no SQL we own.
    Models whose ``raw_code`` is missing or unparseable are listed in the
    report's ``skipped`` field with a reason rather than raising.
    """
    active: list[LocatedFinding] = []
    suppressed: list[SuppressedFinding] = []
    skipped: list[SkippedModel] = []
    scanned = 0
    for _, node in sorted(manifest.models.items()):
        outcome = _scan_one(node, detectors=detectors, dialect=dialect)
        if isinstance(outcome, _Scanned):
            scanned += 1
            active.extend(outcome.findings)
            suppressed.extend(outcome.suppressed)
        else:
            skipped.append(outcome)
    return AuditReport(
        findings=tuple(active),
        suppressed=tuple(suppressed),
        skipped=tuple(skipped),
        models_scanned=scanned,
    )


@dataclass(frozen=True, slots=True)
class _Scanned:
    findings: tuple[LocatedFinding, ...]
    suppressed: tuple[SuppressedFinding, ...]


def _scan_one(
    node: Node,
    *,
    detectors: Sequence[Detector],
    dialect: str | None,
) -> _Scanned | SkippedModel:
    if node.raw_code is None:
        return SkippedModel(unique_id=node.unique_id, reason="no raw_code")
    try:
        parsed = ParsedSQL.parse(node.raw_code, dialect=dialect)
    except SQLParseError as e:
        return SkippedModel(unique_id=node.unique_id, reason=f"parse error: {e}")

    raw_findings: list[Finding] = []
    for detector in detectors:
        raw_findings.extend(detector(parsed))
    directives, malformed = parse_directives(node.raw_code)
    # Malformed-suppression findings ride the regular pipeline; we never
    # suppress them with another directive (it would be silly), so apply()
    # only runs over the detector findings.
    active_raw, suppressed_raw = apply(raw_findings, directives)
    located_active = [_locate(node, f) for f in (*active_raw, *malformed)]
    located_suppressed = [
        SuppressedFinding(
            located=_locate(node, f),
            reason=d.reason,
            directive_line=d.line,
        )
        for f, d in suppressed_raw
    ]
    return _Scanned(
        findings=tuple(located_active),
        suppressed=tuple(located_suppressed),
    )


def _locate(node: Node, finding: Finding) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id=node.unique_id,
        file_path=node.original_file_path,
        finding=finding,
    )
