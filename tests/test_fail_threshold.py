"""The ``--fail-on`` threshold: a run fails iff some unsuppressed finding sits at or
above the chosen level. Below the threshold the run exits clean.

The boundary is tested directly against synthetic findings of known severities so the
contract is deterministic and not coupled to what a fixture happens to contain.
"""

from __future__ import annotations

import pytest

from dblect.audit.walker import LocatedFinding
from dblect.severity import Severity, exceeds_threshold
from dblect.sql import Finding, FindingKind

# A kind at each level so the test names levels, not kinds: JOIN_FANOUT is error,
# UNORDERED_AGGREGATE is warn. There is no native info-level structural kind, so the
# info boundary is exercised by the empty/at-threshold cases.
_ERROR_KIND = FindingKind.JOIN_FANOUT
_WARN_KIND = FindingKind.UNORDERED_AGGREGATE


def _finding(kind: FindingKind) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id="model.p.m",
        file_path="models/m.sql",
        finding=Finding(kind=kind, message="", sql_snippet="", line_start=1, line_end=1),
    )


def test_severity_orders_info_below_warn_below_error() -> None:
    # The threshold rests on this ordering; the comparison follows rank, not the
    # inherited string order, so the boundary tests above are numeric over the levels.
    assert Severity.INFO < Severity.WARN < Severity.ERROR
    assert Severity.ERROR >= Severity.WARN >= Severity.INFO
    assert sorted(Severity) == [Severity.INFO, Severity.WARN, Severity.ERROR]


def test_severity_does_not_compare_against_a_bare_string() -> None:
    # A StrEnum is a str, so a NotImplemented fallback would compare lexicographically
    # ("error" < "info") and quietly invert the ordering. We raise instead.
    with pytest.raises(TypeError):
        _ = Severity.ERROR < "info"


def test_no_findings_never_exceeds_any_threshold() -> None:
    for level in Severity:
        assert exceeds_threshold((), level) is False


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (Severity.INFO, True),
        (Severity.WARN, True),
        (Severity.ERROR, False),
    ],
)
def test_warn_finding_crosses_at_or_below_warn(threshold: Severity, expected: bool) -> None:
    findings = (_finding(_WARN_KIND),)
    assert exceeds_threshold(findings, threshold) is expected


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (Severity.INFO, True),
        (Severity.WARN, True),
        (Severity.ERROR, True),
    ],
)
def test_error_finding_crosses_every_threshold(threshold: Severity, expected: bool) -> None:
    findings = (_finding(_ERROR_KIND),)
    assert exceeds_threshold(findings, threshold) is expected


def test_highest_finding_decides() -> None:
    findings = (_finding(_WARN_KIND), _finding(_ERROR_KIND))
    # A warn-level run plus one error fails an error threshold on the strength of the error.
    assert exceeds_threshold(findings, Severity.ERROR) is True
