"""The findings ``dblect check`` reports, and the report that carries them.

These are declaration-level findings: a contract that does not line up with the
manifest, a declared domain type contradicted by what propagation infers, a sum
the algebra cannot call well typed. They are distinct from the SQL-structural
findings the ``audit`` walker emits, so they carry their own kinds and their own
small report shape rather than borrowing the SQL ``Finding`` (which is a span in
one statement). See ``docs/design/declaration-dsl.md`` and
``docs/design/propagation-soundness.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum, auto

from dblect.audit.sourcemap import SourceSpan
from dblect.check.coverage import SINGLE_WORLD, GroundingCoverage, ResolutionCoverage, WorldCoverage
from dblect.loader import LoadIssue
from dblect.types.bridge import IssueCode


class CheckFindingKind(StrEnum):
    """What a check finding is about."""

    CONTRACT_ISSUE = auto()
    """A contract did not resolve against the manifest (unknown model, unsourced
    field, out-of-domain value, malformed declaration)."""

    DOMAIN_TYPE_CONTRADICTION = auto()
    """A declared domain type is contradicted by the type propagation inferred for
    the same column, and the contradiction shows up at every downstream column it
    reaches (currency creep)."""

    AGGREGATION_NOT_WELL_TYPED = auto()
    """A reduction over one field of a multi-field type whose other fields are not
    provably constant across the group (the mixed-currency sum)."""

    JOIN_KEY_TYPE_MISMATCH = auto()
    """A join's ON-clause equality equates two columns whose domain types meet to a
    conflict (a ``MoneyUSD`` key against a ``MoneyEUR`` one, an ISO-2 country against an
    ISO-3), so the equated values cannot mean the same thing."""

    GRAIN_NOT_ESTABLISHED = auto()
    """A model's SQL carries a strictly finer key than its declared grain (one row
    per order declared; one row per order line produced). The data may still satisfy
    the grain, so this is "not established", not "violated"; see
    ``docs/design/refutation-and-verdicts.md``."""

    RESOLUTION_BELOW_FLOOR = auto()
    """Lineage resolution across the project sits below the configured floor, so
    the analysis covers only a fraction of columns and a clean report would
    overstate what was checked. A capability gap, not a project defect."""

    DEAD_PREDICATE = auto()
    """A literal compared for equality against a column with a declared closed
    set of values (an enum on a contract, an ``accepted_values`` test) is not one
    of those values, so the comparison can never be true. The verdict follows
    from the declaration alone and holds whatever the tables contain."""

    DEAD_PREDICATE_CASE_ONLY = auto()
    """A string literal differs from a declared value only by letter case. On a
    case-sensitive warehouse (Snowflake and DuckDB by default) the comparison is
    as dead as a ``DEAD_PREDICATE``; under a case-insensitive collation it is
    live. Its own kind because the wording and the grade both differ."""

    REDUNDANT_PREDICATE = auto()
    """A ``!=`` or ``NOT IN`` against a value outside the column's declared set
    excludes nothing a real value could have matched, so the filter removes only
    NULL rows. The query is not wrong, only unfiltered: a real bug, but not a
    wrong-rows one."""

    CASE_LEAVES_ENUM_MEMBER_UNHANDLED = auto()
    """A ``CASE`` with several arms over a column with a declared closed set
    tests some of the values and not others, so an untested value falls silently
    to the default, whether that default is absent, ``ELSE NULL``, or a literal.
    A remap may lump a value into its default on purpose, so this says
    "unhandled", never "wrong"."""


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
        return self.compiled_span

    @property
    def compiled_span(self) -> SourceSpan:
        """The raw compiled coordinate the derivation node was stamped in, the frame a
        macro body's ``-- noqa`` is matched against."""
        return SourceSpan.compiled(self.line_start, self.line_end)


@dataclass(frozen=True, slots=True)
class SuppressedCheckFinding:
    """A declaration-level finding a ``-- noqa`` directive silenced. ``directive_line``
    is where the directive sat; ``bare`` records whether it was a bare ``-- noqa`` (all
    kinds) rather than a code-specific one; ``directive_in_compiled`` records whether the
    directive was read in the compiled frame (a macro body's ``-- noqa``)."""

    finding: CheckFinding
    directive_line: int
    bare: bool
    directive_in_compiled: bool = False


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
