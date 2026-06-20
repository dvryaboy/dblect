"""Contracts for the intent-dependent classification and the suppression hint.

The hint points the reader at the `-- noqa-fixture:` mechanism for findings whose
right resolution is "confirm it's deliberate and record why". These pin three
things: the membership set is total over `FindingKind`, the hint carries the
finding's own kind, and the line it suggests is one the suppression scanner
actually accepts (round-trip, not just plausible prose).
"""

from __future__ import annotations

import pytest

from dblect.audit.suppress import parse_directives
from dblect.sql import FindingKind, is_intent_dependent, suppression_hint
from dblect.sql.patterns import INTENT_DEPENDENT_KINDS

# The intent-dependent kinds, pinned literally so a change to the set is a
# deliberate edit to this test rather than a silent drift.
_EXPECTED_INTENT_DEPENDENT: frozenset[FindingKind] = frozenset(
    {
        FindingKind.NON_DETERMINISTIC_FUNCTION,
        FindingKind.COALESCE_ON_JOIN_KEY,
        FindingKind.UNORDERED_RANKING_WINDOW,
        FindingKind.UNORDERED_AGGREGATE,
    }
)

_INTENT_DEPENDENT_SORTED: list[FindingKind] = sorted(INTENT_DEPENDENT_KINDS, key=lambda k: k.value)


def test_membership_set_is_exactly_the_intent_dependent_kinds() -> None:
    assert INTENT_DEPENDENT_KINDS == _EXPECTED_INTENT_DEPENDENT


@pytest.mark.parametrize("kind", list(FindingKind))
def test_classification_is_total_over_finding_kind(kind: FindingKind) -> None:
    # Every kind is classified intent-dependent or not. A new kind added to the
    # enum without a decision here shows up as a membership it did not expect.
    assert is_intent_dependent(kind) is (kind in _EXPECTED_INTENT_DEPENDENT)


def test_malformed_suppression_is_never_intent_dependent() -> None:
    # An always-a-bug kind has no "intentional" form to record.
    assert not is_intent_dependent(FindingKind.MALFORMED_SUPPRESSION)


@pytest.mark.parametrize("kind", _INTENT_DEPENDENT_SORTED)
def test_hint_carries_the_findings_own_kind(kind: FindingKind) -> None:
    hint = suppression_hint(kind)
    assert "noqa-fixture" in hint
    assert kind.value in hint


@pytest.mark.parametrize("kind", _INTENT_DEPENDENT_SORTED)
def test_hint_round_trips_through_the_suppression_scanner(kind: FindingKind) -> None:
    # The line the hint tells the user to write must be one the scanner accepts
    # for this exact kind. We materialise the hint's `<reason>` placeholder into a
    # real reason and parse the resulting comment.
    hint = suppression_hint(kind)
    directive_line = hint.split("`")[1].replace("<reason>", "recorded as deliberate")
    directives, malformed = parse_directives(directive_line + "\n")
    assert malformed == ()
    [directive] = directives
    assert directive.kind is kind
    assert directive.reason == "recorded as deliberate"
