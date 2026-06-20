"""Contracts for the `-- noqa-fixture:` suppression hint.

The hint points the reader at the suppression mechanism, which silences any
structural finding by line. These pin two things: the hint carries the finding's
own kind, and the line it suggests is one the suppression scanner actually accepts
(a round-trip, not just plausible prose).
"""

from __future__ import annotations

import pytest

from dblect.audit.suppress import parse_directives
from dblect.sql import FindingKind, suppression_hint


@pytest.mark.parametrize("kind", list(FindingKind))
def test_hint_carries_the_findings_own_kind(kind: FindingKind) -> None:
    hint = suppression_hint(kind)
    assert "noqa-fixture" in hint
    assert kind.value in hint


@pytest.mark.parametrize("kind", list(FindingKind))
def test_hint_round_trips_through_the_suppression_scanner(kind: FindingKind) -> None:
    # The line the hint tells the user to write must be one the scanner accepts for
    # this exact kind. We materialise the hint's `<reason>` placeholder into a real
    # reason and parse the resulting comment.
    hint = suppression_hint(kind)
    directive_line = hint.split("`")[1].replace("<reason>", "recorded as deliberate")
    directives, malformed = parse_directives(directive_line + "\n")
    assert malformed == ()
    [directive] = directives
    assert directive.kind is kind
    assert directive.reason == "recorded as deliberate"
