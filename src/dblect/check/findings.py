"""The findings ``dblect check`` reports, and the report that carries them.

These are declaration-level findings: a contract that does not line up with the
manifest, a declared domain type contradicted by what the substrate propagates, a
sum the algebra cannot call well typed. They are distinct from the SQL-structural
findings the ``audit`` walker emits, so they carry their own kinds and their own
small report shape rather than borrowing the SQL ``Finding`` (which is a span in
one statement). See ``docs/design/declaration-dsl.md`` and
``docs/design/propagation-soundness.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum, auto

from dblect.audit.sourcemap import SourceSpan, SpanBasis
from dblect.check.coverage import SINGLE_WORLD, GroundingCoverage, ResolutionCoverage, WorldCoverage
from dblect.loader import LoadIssue
from dblect.types.bridge import IssueCode


class CheckFindingKind(StrEnum):
    """What a check finding is about."""

    CONTRACT_ISSUE = auto()
    """A contract did not resolve against the manifest (unknown model, unsourced
    field, out-of-domain value, malformed declaration)."""

    DOMAIN_TYPE_CONTRADICTION = auto()
    """A declared domain type is contradicted by the type the substrate inferred
    for the same column, and the contradiction rides the DAG (currency creep)."""

    AGGREGATION_NOT_WELL_TYPED = auto()
    """A reduction over one field of a multi-field type whose other fields are not
    provably constant across the group (the mixed-currency sum)."""

    RESOLUTION_BELOW_FLOOR = auto()
    """Lineage resolution across the project sits below the configured floor, so
    the analysis covers only a fraction of columns and a clean report would
    overstate what was checked. A capability gap, not a project defect."""


@dataclass(frozen=True, slots=True)
class CheckFinding:
    """One declaration-level finding, located on the model it lands on."""

    kind: CheckFindingKind
    message: str
    model_unique_id: str | None
    file_path: str | None = None
    column: str | None = None
    contract: str | None = None
    code: IssueCode | None = None
    """The specific resolution cause behind a ``CONTRACT_ISSUE``; ``None`` for the
    other kinds, which carry no such code."""
    line_start: int = 0
    line_end: int = 0
    """1-indexed span of the offending projection or aggregate in the model's
    **compiled** SQL (the line space the derivation node was stamped in). ``0`` means
    we could not pin it to a line (a contract-resolution issue or a project-wide
    coverage finding has no single SQL site), and a finding with no line is never
    line-suppressible. The same convention the structural ``Finding`` uses, so one
    suppression scanner serves both families."""
    source_span: SourceSpan | None = None
    """The compiled span back-mapped onto the on-disk template, set by the check run for
    the line-located kinds and ``None`` for an unlocated finding or one built outside
    the run."""

    @property
    def located_span(self) -> SourceSpan:
        """The span to report: the back-mapped ``source_span``, or the compiled span as a
        compiled-relative fallback when none is attached."""
        if self.source_span is not None:
            return self.source_span
        return SourceSpan(self.line_start, self.line_end, SpanBasis.COMPILED)


@dataclass(frozen=True, slots=True)
class SuppressedCheckFinding:
    """A declaration-level finding a ``-- noqa`` directive silenced. ``directive_line``
    is where the directive sat; ``bare`` records whether it was a bare ``-- noqa`` (all
    kinds) rather than a code-specific one."""

    finding: CheckFinding
    directive_line: int
    bare: bool


@dataclass(frozen=True, slots=True)
class UnbuiltModel:
    """A model dblect could not analyze (no compiled SQL, or a parse/qualify
    failure), with the reason. Surfaced so a model the analysis could not read is
    never mistaken for one it read and found clean."""

    unique_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class CheckReport:
    """The output of one ``run_check``: findings, the modules that failed to load,
    the models that could not be analyzed, and a few counts for the summary line."""

    findings: tuple[CheckFinding, ...]
    load_issues: tuple[LoadIssue, ...]
    unbuilt: tuple[UnbuiltModel, ...]
    contracts_resolved: int
    models_propagated: int
    predicates_collected: int
    suppressed: tuple[SuppressedCheckFinding, ...] = ()
    resolution: ResolutionCoverage = field(default_factory=lambda: ResolutionCoverage(0, 0, 0, ()))
    grounding: GroundingCoverage = field(default_factory=lambda: GroundingCoverage((), 0, 0))
    worlds: WorldCoverage = SINGLE_WORLD

    @property
    def has_findings(self) -> bool:
        return bool(self.findings) or bool(self.load_issues)

    @property
    def models_analyzed(self) -> int:
        return self.models_propagated - len(self.unbuilt)
