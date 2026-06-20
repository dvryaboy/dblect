"""Severity is a real ordering, every finding kind maps to one, and ``severity_of``
reads a finding's level without the caller knowing which family it came from.

These pin the contracts the ``--fail-on`` threshold rests on: the mapping is total
(a new kind without a level is a test failure, not a silent ``info``), the levels
order so ``>=`` is well typed, and the two families both answer ``severity_of``.
"""

from __future__ import annotations

from dblect.audit.walker import LocatedFinding
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.severity import (
    CHECK_FINDING_SEVERITY,
    STRUCTURAL_FINDING_SEVERITY,
    Severity,
    severity_of,
)
from dblect.sql import Finding, FindingKind


def test_severity_orders_info_below_warn_below_error() -> None:
    assert Severity.INFO < Severity.WARN < Severity.ERROR
    assert Severity.ERROR >= Severity.WARN >= Severity.INFO
    assert sorted(Severity) == [Severity.INFO, Severity.WARN, Severity.ERROR]


def test_every_structural_kind_has_a_severity() -> None:
    missing = [k for k in FindingKind if k not in STRUCTURAL_FINDING_SEVERITY]
    assert missing == [], f"FindingKind without a severity: {missing}"
    assert set(STRUCTURAL_FINDING_SEVERITY) == set(FindingKind)


def test_every_check_kind_has_a_severity() -> None:
    missing = [k for k in CheckFindingKind if k not in CHECK_FINDING_SEVERITY]
    assert missing == [], f"CheckFindingKind without a severity: {missing}"
    assert set(CHECK_FINDING_SEVERITY) == set(CheckFindingKind)


def test_intent_anchors_from_the_issue() -> None:
    # The two examples the issue pins by name; the rest is the table's own contract.
    assert STRUCTURAL_FINDING_SEVERITY[FindingKind.JOIN_FANOUT] is Severity.ERROR
    assert STRUCTURAL_FINDING_SEVERITY[FindingKind.UNORDERED_AGGREGATE] is Severity.WARN
    assert STRUCTURAL_FINDING_SEVERITY[FindingKind.MALFORMED_SUPPRESSION] is Severity.WARN


def _structural(kind: FindingKind) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id="model.p.m",
        file_path="models/m.sql",
        finding=Finding(kind=kind, message="", sql_snippet="", line_start=1, line_end=1),
    )


def _declaration(kind: CheckFindingKind) -> CheckFinding:
    return CheckFinding(kind=kind, message="", model_unique_id="model.p.m")


def test_severity_of_reads_both_families() -> None:
    for kind in FindingKind:
        assert severity_of(_structural(kind)) is STRUCTURAL_FINDING_SEVERITY[kind]
    for kind in CheckFindingKind:
        assert severity_of(_declaration(kind)) is CHECK_FINDING_SEVERITY[kind]


def test_severity_parses_from_its_value() -> None:
    assert Severity("warn") is Severity.WARN
    assert Severity.ERROR.value == "error"
