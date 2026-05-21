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

from dblect.manifest import Manifest, Node
from dblect.sql import (
    Finding,
    FindingKind,
    ParsedSQL,
    SQLParseError,
    detect_coalesce_on_join_key,
    detect_null_group_after_outer_join,
    detect_unordered_aggregate,
    detect_unordered_window,
)

Detector = Callable[[ParsedSQL], tuple[Finding, ...]]

DEFAULT_DETECTORS: tuple[Detector, ...] = (
    detect_null_group_after_outer_join,
    detect_coalesce_on_join_key,
    detect_unordered_window,
    detect_unordered_aggregate,
)


@dataclass(frozen=True, slots=True)
class LocatedFinding:
    """A `Finding` plus the model and file it came from."""

    model_unique_id: str
    file_path: str | None
    finding: Finding


@dataclass(frozen=True, slots=True)
class SkippedModel:
    """A model the walker couldn't scan, with the reason."""

    unique_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class AuditReport:
    """The output of one ``run_audit`` invocation."""

    findings: tuple[LocatedFinding, ...]
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
    findings: list[LocatedFinding] = []
    skipped: list[SkippedModel] = []
    scanned = 0
    for _, node in sorted(manifest.models.items()):
        outcome = _scan_one(node, detectors=detectors, dialect=dialect)
        if isinstance(outcome, _Scanned):
            scanned += 1
            findings.extend(outcome.findings)
        else:
            skipped.append(outcome)
    return AuditReport(
        findings=tuple(findings),
        skipped=tuple(skipped),
        models_scanned=scanned,
    )


@dataclass(frozen=True, slots=True)
class _Scanned:
    findings: tuple[LocatedFinding, ...]


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
    located: list[LocatedFinding] = []
    for detector in detectors:
        located.extend(
            LocatedFinding(
                model_unique_id=node.unique_id,
                file_path=node.original_file_path,
                finding=f,
            )
            for f in detector(parsed)
        )
    return _Scanned(findings=tuple(located))
