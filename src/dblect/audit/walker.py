"""Run SQL detectors over every model in a manifest, return a typed report.

The walker is the substrate the CLI sits on. It iterates ``manifest.models``,
feeds each model's compiled SQL (rendered by ``dbt compile``) into the SQL
parser, runs the configured detectors, and attaches each finding to its
originating model and source file path.

Models whose analysis SQL is absent and models whose SQL fails to parse are
recorded on the report as skipped, with a reason. The walker never raises on
per-model failure: one bad model shouldn't blind the audit to the rest of the
project.

Suppression directives (``-- noqa-fixture:`` comments) are always read from
``raw_code``: they live in the source the developer wrote, not in the
compiled output.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlglot import Expr

from dblect.audit.suppress import apply, parse_directives
from dblect.manifest import Manifest, Node
from dblect.sql import (
    Finding,
    FindingKind,
    SQLParseError,
    detect_coalesce_on_join_key,
    detect_non_deterministic_function,
    detect_null_group_after_outer_join,
    detect_unordered_aggregate,
    detect_unordered_window,
    detect_where_on_outer_joined_nullable,
    parse_sql,
)
from dblect.uniqueness import facts_from_manifest
from dblect.uniqueness.detector import make_detector as _make_uniqueness_detector

Detector = Callable[[Expr], tuple[Finding, ...]]

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
    Models whose ``compiled_code`` is missing or unparseable are listed in
    the report's ``skipped`` field with a reason rather than raising.

    A uniqueness-aware detector (window order-keys grounded against declared
    keys on the source model) runs alongside the configured `detectors` list.
    It's silent on projects without declared uniqueness facts, so it doesn't
    need an opt-in flag.
    """
    parsed = _parse_models_for_audit(manifest, dialect=dialect)
    trees = {uid: t for uid, t in parsed.items() if isinstance(t, Expr)}
    facts = facts_from_manifest(manifest, parsed=trees)
    uniqueness_detector = _make_uniqueness_detector(manifest, facts)
    effective_detectors: tuple[Detector, ...] = (*tuple(detectors), uniqueness_detector)
    active: list[LocatedFinding] = []
    suppressed: list[SuppressedFinding] = []
    skipped: list[SkippedModel] = []
    scanned = 0
    for uid, node in sorted(manifest.models.items()):
        outcome = _scan_one(node, parsed.get(uid), detectors=effective_detectors)
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


def _parse_models_for_audit(
    manifest: Manifest, *, dialect: str | None
) -> Mapping[str, Expr | SQLParseError]:
    """Parse each model's analysis SQL exactly once.

    Returns a mapping from model unique_id to either the parsed tree or the
    `SQLParseError` that prevented parsing. Models with no analysis SQL are
    absent from the mapping; the walker treats absence as "no compiled SQL"
    when it iterates.
    """
    out: dict[str, Expr | SQLParseError] = {}
    for uid, model in manifest.models.items():
        sql = model.analysis_sql
        if sql is None:
            continue
        try:
            out[uid] = parse_sql(sql, dialect=dialect)
        except SQLParseError as e:
            out[uid] = e
    return out


@dataclass(frozen=True, slots=True)
class _Scanned:
    findings: tuple[LocatedFinding, ...]
    suppressed: tuple[SuppressedFinding, ...]


def _scan_one(
    node: Node,
    parse_outcome: Expr | SQLParseError | None,
    *,
    detectors: Sequence[Detector],
) -> _Scanned | SkippedModel:
    if parse_outcome is None:
        return SkippedModel(
            unique_id=node.unique_id,
            reason="no compiled SQL (run `dbt compile`)",
        )
    if isinstance(parse_outcome, SQLParseError):
        return SkippedModel(unique_id=node.unique_id, reason=f"parse error: {parse_outcome}")
    tree = parse_outcome

    raw_findings: list[Finding] = []
    for detector in detectors:
        raw_findings.extend(detector(tree))
    # Directives live in the source the developer wrote, not the compiled
    # output. Fall back to the compiled SQL only if `raw_code` is missing.
    directives, malformed = parse_directives(node.raw_code or node.analysis_sql or "")
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
