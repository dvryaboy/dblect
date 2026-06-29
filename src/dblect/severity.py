"""A finding's severity, and the threshold a CI run fails at.

Findings used to be flat: any unsuppressed one failed the run. Severity gives each
finding kind a level drawn from the detector's intent, so a run can pick the bar it
fails at. A correctness hazard (the analysis says the query returns wrong rows) is an
``error``; a determinism smell (a result that is correct but order-dependent, so it can
drift between runs) is a ``warn``; an ``info`` is an observation worth surfacing that on
its own should not fail anyone's build.

``Severity`` is a ``StrEnum`` so the level values read as the plain words (the CLI flag
and the JSON field both want ``info``/``warn``/``error``). Comparison is overridden to
follow an explicit rank rather than the inherited string order, so ``>=`` is a real
ordering and not a lexicographic accident; comparing a ``Severity`` to anything else
raises rather than silently falling back to ``str`` order. The per-kind mapping is the
single place a kind's default level is decided, written as a ``match`` closed by
``assert_never`` so a new kind without a level is a type error rather than a silent
default; ``severity_of`` reads a finding's level without the caller knowing which
detector family produced it.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import assert_never

from dblect.analysis import AnalysisFinding
from dblect.audit.walker import LocatedFinding
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.sql import FindingKind


class Severity(StrEnum):
    """An ordered finding level: ``INFO`` < ``WARN`` < ``ERROR``.

    The value is the plain word so the CLI flag and JSON field carry it directly, and
    ``Severity("warn")`` parses back. Comparison follows ``_RANK`` rather than the
    string value, so a threshold test is a numeric one over the levels.
    """

    INFO = "info"
    WARN = "warn"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return _RANK[self]

    # Comparison is by rank, and only against another Severity. Returning
    # NotImplemented here would let Python fall back to ``str``'s lexicographic order
    # (since a StrEnum *is* a str), which would compare ``"error" < "info"`` as True
    # and quietly reintroduce the accident this ordering exists to avoid. We raise.
    def __lt__(self, other: object) -> bool:
        return self.rank < _require_severity(other).rank

    def __le__(self, other: object) -> bool:
        return self.rank <= _require_severity(other).rank

    def __gt__(self, other: object) -> bool:
        return self.rank > _require_severity(other).rank

    def __ge__(self, other: object) -> bool:
        return self.rank >= _require_severity(other).rank


def _require_severity(value: object) -> Severity:
    if not isinstance(value, Severity):
        raise TypeError(
            f"Severity is only comparable to another Severity, not {type(value).__name__}"
        )
    return value


_RANK: dict[Severity, int] = {Severity.INFO: 0, Severity.WARN: 1, Severity.ERROR: 2}


def _structural_severity(kind: FindingKind) -> Severity:
    """A structural finding's default severity. error: the query can return wrong rows.
    warn: the rows are right but their order or value is not pinned, so they can drift."""
    match kind:
        case (
            FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
            | FindingKind.COALESCE_ON_JOIN_KEY
            | FindingKind.WHERE_ON_OUTER_JOINED_NULLABLE
            | FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS
            | FindingKind.NON_UNIQUE_AGGREGATE_ORDER_KEYS
            | FindingKind.JOIN_FANOUT
            | FindingKind.CROSS_MODEL_FANOUT
            | FindingKind.NULL_GROUP_ON_NULLABLE_KEY
            | FindingKind.JOIN_ON_NULLABLE_KEY
            | FindingKind.NOT_IN_NULLABLE_SUBQUERY
            | FindingKind.INNER_FLATTEN_ROW_DROP
            | FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING
            | FindingKind.LIMIT_WITHOUT_DETERMINISTIC_ORDER
        ):
            return Severity.ERROR
        case (
            FindingKind.UNORDERED_RANKING_WINDOW
            | FindingKind.UNORDERED_AGGREGATE
            | FindingKind.NON_DETERMINISTIC_FUNCTION
        ):
            return Severity.WARN
    assert_never(kind)


def _check_severity(kind: CheckFindingKind) -> Severity:
    """A declaration finding's default severity. error: the declared meaning and the
    computed one disagree. warn: the analysis could not see enough to judge."""
    match kind:
        case (
            CheckFindingKind.CONTRACT_ISSUE
            | CheckFindingKind.DOMAIN_TYPE_CONTRADICTION
            | CheckFindingKind.AGGREGATION_NOT_WELL_TYPED
            | CheckFindingKind.JOIN_KEY_TYPE_MISMATCH
        ):
            return Severity.ERROR
        # A coverage gap, warned so thin coverage is visible without failing a run that
        # declared a floor to learn its coverage.
        case CheckFindingKind.RESOLUTION_BELOW_FLOOR:
            return Severity.WARN
    assert_never(kind)


def severity_of(finding: AnalysisFinding) -> Severity:
    """The severity of ``finding``, by its kind, across both detector families.

    Closed by ``assert_never`` so a new finding family is a type error here rather than
    a finding that silently lands at some default level.
    """
    match finding:
        case CheckFinding():
            return _check_severity(finding.kind)
        case LocatedFinding():
            return _structural_severity(finding.finding.kind)
    assert_never(finding)


def exceeds_threshold(findings: Iterable[AnalysisFinding], threshold: Severity) -> bool:
    """True iff some finding sits at or above ``threshold``. The empty run is False."""
    return any(severity_of(f) >= threshold for f in findings)
