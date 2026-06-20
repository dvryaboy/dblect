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
ordering and not a lexicographic accident. The per-kind tables are the single place a
kind's default level is decided; ``severity_of`` reads a finding's level without the
caller knowing which detector family produced it.
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

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank >= other.rank


_RANK: dict[Severity, int] = {Severity.INFO: 0, Severity.WARN: 1, Severity.ERROR: 2}


# A structural finding's default severity, by kind, in one place so the level is a
# property of the detector's intent rather than scattered across call sites.
#
# error: the analysis is saying the query can return wrong rows. A join can fan out and
# multiply measures; an outer join's NULLs leak into a grouping, a coalesce, a join key,
# a NOT IN, or a comparison that silently turns the outer join inner; a window's order
# keys are not unique so the pick is arbitrary; a snapshot read has no temporal filter so
# it sees every version. Each changes the result set, not just its order.
#
# warn: the result is correct but its order or value is not pinned, so it can drift
# between runs. An unordered ranking window or aggregate, and a non-deterministic builtin
# in a load-bearing position, are determinism smells. A malformed suppression directive is
# an operator mistake worth surfacing, not a query defect, so it warns rather than errors.
STRUCTURAL_FINDING_SEVERITY: dict[FindingKind, Severity] = {
    FindingKind.NULL_GROUP_AFTER_OUTER_JOIN: Severity.ERROR,
    FindingKind.COALESCE_ON_JOIN_KEY: Severity.ERROR,
    FindingKind.WHERE_ON_OUTER_JOINED_NULLABLE: Severity.ERROR,
    FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS: Severity.ERROR,
    FindingKind.JOIN_FANOUT: Severity.ERROR,
    FindingKind.NULL_GROUP_ON_NULLABLE_KEY: Severity.ERROR,
    FindingKind.JOIN_ON_NULLABLE_KEY: Severity.ERROR,
    FindingKind.NOT_IN_NULLABLE_SUBQUERY: Severity.ERROR,
    FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING: Severity.ERROR,
    FindingKind.UNORDERED_RANKING_WINDOW: Severity.WARN,
    FindingKind.UNORDERED_AGGREGATE: Severity.WARN,
    FindingKind.NON_DETERMINISTIC_FUNCTION: Severity.WARN,
    FindingKind.MALFORMED_SUPPRESSION: Severity.WARN,
}

# A declaration finding's default severity, by kind. A contract that does not line up
# with the manifest, a declared domain type the substrate contradicts, and a sum the
# algebra cannot call well typed are each a statement that the declared meaning and the
# computed one disagree, an error. A resolution that sits below the configured floor is a
# coverage gap (the analysis could not see enough to judge), surfaced as a warn so thin
# coverage is visible without failing a run that declared a floor to learn its coverage.
CHECK_FINDING_SEVERITY: dict[CheckFindingKind, Severity] = {
    CheckFindingKind.CONTRACT_ISSUE: Severity.ERROR,
    CheckFindingKind.DOMAIN_TYPE_CONTRADICTION: Severity.ERROR,
    CheckFindingKind.AGGREGATION_NOT_WELL_TYPED: Severity.ERROR,
    CheckFindingKind.RESOLUTION_BELOW_FLOOR: Severity.WARN,
}


def severity_of(finding: AnalysisFinding) -> Severity:
    """The severity of ``finding``, read from its kind's table, across both families.

    Closed by ``assert_never`` so a new finding family is a type error here rather than
    a finding that silently lands at some default level.
    """
    match finding:
        case CheckFinding():
            return CHECK_FINDING_SEVERITY[finding.kind]
        case LocatedFinding():
            return STRUCTURAL_FINDING_SEVERITY[finding.finding.kind]
    assert_never(finding)


def exceeds_threshold(findings: Iterable[AnalysisFinding], threshold: Severity) -> bool:
    """True iff some finding sits at or above ``threshold``. The empty run is False."""
    return any(severity_of(f) >= threshold for f in findings)
