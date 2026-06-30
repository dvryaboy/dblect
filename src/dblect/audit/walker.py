"""Run SQL detectors over every model in a manifest, return a typed report.

The walker is the substrate the CLI sits on. It iterates ``manifest.models``,
feeds each model's compiled SQL (rendered by ``dbt compile``) into the SQL
parser, runs the configured detectors, and attaches each finding to its
originating model and source file path.

Models whose analysis SQL is absent and models whose SQL fails to parse are
recorded on the report as skipped, with a reason. The walker never raises on
per-model failure: one bad model shouldn't blind the audit to the rest of the
project.

Suppression directives (``-- noqa`` comments) are read from both the developer's
template and the compiled SQL: a finding the back-map placed on a source line is
silenced from the template, and one that stayed compiled-relative (a construct emitted
inside a macro body) is silenced from the compiled output, where the macro's own
``-- noqa`` renders next to the construct. The finding's span basis picks the frame.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.audit.incremental import incremental_findings
from dblect.audit.sourcemap import LineMap, SourceSpan, build_line_map
from dblect.audit.suppress import FramedDirectives, apply
from dblect.flatten.detector import make_array_nonemptiness_detectors
from dblect.lineage.builder import build_manifest_graph
from dblect.manifest import Manifest, Node, compilation_miss_reason
from dblect.nullability.detector import make_nullability_detectors
from dblect.snapshot import make_snapshot_detectors
from dblect.sql import (
    Finding,
    FindingKind,
    SQLParseError,
    detect_coalesce_on_join_key,
    detect_null_group_after_outer_join,
    detect_unordered_aggregate,
    detect_unordered_window,
    detect_where_on_outer_joined_nullable,
    make_non_determinism_detector,
    parse_models,
)
from dblect.uniqueness.detector import (
    make_cross_model_fanout_detectors,
    make_fact_grounded_detectors,
    relation_uniqueness,
)

Detector = Callable[[Expr], tuple[Finding, ...]]

# The dialect-agnostic structural detectors. Context-bound detectors (non-determinism
# from the resolved adapter, uniqueness and nullability grounded against declared
# facts, inner-flatten grounded against propagated array non-emptiness) are built per
# run in `run_audit` and appended there, so they are not listed. The inner-flatten check
# in particular is run only through its fact-grounded factory so it sees the cross-model
# non-emptiness map; listing the structural form here too would double-report it.
DEFAULT_DETECTORS: tuple[Detector, ...] = (
    detect_null_group_after_outer_join,
    detect_coalesce_on_join_key,
    detect_unordered_window,
    detect_unordered_aggregate,
    detect_where_on_outer_joined_nullable,
)


@dataclass(frozen=True, slots=True)
class LocatedFinding:
    """A `Finding` plus the model and file it came from.

    ``finding`` carries the compiled-SQL span the parser produced (the raw
    observation); ``source_span`` is that span back-mapped onto the on-disk template,
    set by the walker and ``None`` when no back-map was performed.
    """

    model_unique_id: str
    file_path: str | None
    finding: Finding
    source_span: SourceSpan | None = None

    @property
    def kind(self) -> FindingKind:
        """The finding's kind, surfaced so a ``-- noqa`` directive can match it through the
        ``Suppressible`` protocol the same way a declaration finding's does."""
        return self.finding.kind

    @property
    def located_span(self) -> SourceSpan:
        """The span to report: the back-mapped ``source_span``, or the compiled span as a
        compiled-relative fallback when none is attached."""
        if self.source_span is not None:
            return self.source_span
        return self.compiled_span

    @property
    def compiled_span(self) -> SourceSpan:
        """The raw compiled coordinate the parser observed, the frame a macro body's
        ``-- noqa`` is matched against."""
        return SourceSpan.compiled(self.finding.line_start, self.finding.line_end)


@dataclass(frozen=True, slots=True)
class SuppressedFinding:
    """A finding that a ``-- noqa`` directive silenced. ``directive_line`` is where the
    directive sat; ``bare`` records whether it was a bare ``-- noqa`` (all kinds) rather
    than a code-specific one; ``directive_in_compiled`` records whether the directive was
    read in the compiled frame (a macro body's ``-- noqa``), so the report can label its
    line as compiled space rather than a source line."""

    located: LocatedFinding
    directive_line: int
    bare: bool
    directive_in_compiled: bool = False


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
    profile: AdapterProfile,
    *,
    detectors: Sequence[Detector] = DEFAULT_DETECTORS,
) -> AuditReport:
    """Run `detectors` over every model in `manifest`.

    ``profile`` is the run's resolved target: its dialect parses every model and
    its semantics ground the fact-based detectors, so parsing and enforcement read
    the same adapter.

    Sources, seeds, and snapshots are not scanned: they have no SQL we own.
    Models whose ``compiled_code`` is missing or unparseable are listed in
    the report's ``skipped`` field with a reason rather than raising.

    Context-bound detectors run alongside the configured `detectors` list, each
    built from the resolved profile and the pre-parsed trees: the non-determinism
    detector (its builtin names come from the profile), the uniqueness window
    order-keys, join-fanout, and non-deterministic-``LIMIT`` detectors (grounded
    against declared keys, the last also against each model's materialization), the
    nullability hazard detectors (GROUP BY, join key, and NOT IN on an
    inherited-nullable column, grounded against the propagated nullability
    property), the inner-flatten row-drop detector (grounded against the
    propagated array-non-emptiness property, so a rebuilt-then-unnested array is
    not flagged), and the snapshot temporal-filter detector (grounded against the
    manifest's snapshots and their validity columns). The fact-grounded ones are
    opportunistic, silent on projects that declare nothing, so they need no opt-in
    flag. They share the audit's pre-parsed trees, so the SQL is parsed once.

    This is the structural family alone. A consumer that needs every family's
    findings over a manifest (any multi-world or finding-threading path) calls
    :func:`dblect.analysis.analyze` instead, which carries both families so a family
    is never dropped by being forgotten.
    """
    parsed = parse_models(
        {uid: m.analysis_sql for uid, m in manifest.models.items()},
        dialect=profile.sqlglot_dialect,
    )
    trees = {uid: t for uid, t in parsed.items() if isinstance(t, Expr)}
    # The fact-grounded and cross-model fan-out factories both rest on the relation graph's
    # propagated uniqueness; propagate it once and share it so the fixpoint runs a single time.
    rel_keys = relation_uniqueness(manifest, profile, parsed=trees)
    # The column graph is the heavy shared substrate: qualifying and scope-resolving every
    # model (which is also what stamps the shared trees with their resolved column refs).
    # Build it once and hand it to each column-graph fact family so the walk runs a single
    # time per audit rather than once per family.
    col_graph = build_manifest_graph(manifest, dialect=profile.sqlglot_dialect, parsed=trees).graph
    contextual: tuple[Detector, ...] = (
        make_non_determinism_detector(profile.non_deterministic_builtins),
        *make_fact_grounded_detectors(manifest, profile, parsed=trees, relation_keys=rel_keys),
        *make_cross_model_fanout_detectors(
            manifest, profile, parsed=trees, relation_keys=rel_keys, column_graph=col_graph
        ),
        *make_nullability_detectors(manifest, profile, parsed=trees, column_graph=col_graph),
        *make_array_nonemptiness_detectors(manifest, profile, parsed=trees, column_graph=col_graph),
        *make_snapshot_detectors(manifest),
    )
    effective_detectors: tuple[Detector, ...] = (*tuple(detectors), *contextual)
    active: list[LocatedFinding] = []
    suppressed: list[SuppressedFinding] = []
    skipped: list[SkippedModel] = []
    scanned = 0
    for uid, node in sorted(manifest.models.items()):
        # The incremental-config check reads the manifest, not the parsed SQL, so it runs
        # for every model independent of whether the SQL compiled and parsed.
        config_scan = _config_scan(node)
        active.extend(config_scan.findings)
        suppressed.extend(config_scan.suppressed)
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
    compilation_reason = compilation_miss_reason(node.compilation_status)
    if compilation_reason is not None:
        return SkippedModel(unique_id=node.unique_id, reason=compilation_reason)
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
    # Findings carry compiled-SQL spans; back-map them onto the source template once per
    # model, then match directives against the located span. Locating before suppressing
    # is what lets a `-- noqa` on the line the report shows silence a finding whose
    # compiled line a macro expansion pushed away from its source line.
    line_map = build_line_map(node.analysis_sql, node.raw_code)
    located = [_locate(node, f, line_map) for f in raw_findings]
    return _finalize(node, located)


def _config_scan(node: Node) -> _Scanned:
    """The manifest-level findings for `node`: the incremental-config check, which reasons
    over the node's config rather than its SQL. Model-scoped (line 0) and run through the
    same suppression as the SQL findings, so the report shape stays uniform."""
    located = [
        LocatedFinding(model_unique_id=node.unique_id, file_path=node.original_file_path, finding=f)
        for f in incremental_findings(node)
    ]
    return _finalize(node, located)


def _finalize(node: Node, located: Sequence[LocatedFinding]) -> _Scanned:
    """Apply `node`'s ``-- noqa`` directives to its located findings, partitioning into
    active and suppressed. The single place the directive-match-and-record flow lives, so
    the SQL scan and the manifest-config scan suppress findings the same way.

    Directives are read from both texts: the template for a finding the back-map placed on
    a source line, and the compiled SQL for one a macro emitted, whose guarding `-- noqa`
    renders only into the compiled output. Each finding is matched in the frame(s) it
    occupies.
    """
    directives = FramedDirectives.for_node(node)
    active, suppressed = apply(located, directives)
    located_suppressed = [
        SuppressedFinding(
            located=lf, directive_line=d.line, bare=d.kinds is None, directive_in_compiled=ic
        )
        for lf, d, ic in suppressed
    ]
    return _Scanned(findings=tuple(active), suppressed=tuple(located_suppressed))


def _locate(node: Node, finding: Finding, line_map: LineMap) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id=node.unique_id,
        file_path=node.original_file_path,
        finding=finding,
        source_span=line_map.map_span(finding.line_start, finding.line_end),
    )
