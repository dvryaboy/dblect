"""The suppression hint must be copy-pasteable: the directive it suggests has to
parse back to the same finding kind through the real scanner, not just read like it."""

from __future__ import annotations

from dblect.audit.suppress import parse_directives
from dblect.sql import FindingKind, suppression_code, suppression_hint


def test_hint_names_the_dblect_code() -> None:
    kind = FindingKind.UNORDERED_AGGREGATE
    assert f"-- noqa: {suppression_code(kind)}" in suppression_hint(kind)


def test_suggested_directive_round_trips_through_the_scanner() -> None:
    kind = FindingKind.UNORDERED_AGGREGATE
    directive_line = suppression_hint(kind).split("`")[1]
    [directive] = parse_directives(directive_line + "\n")
    assert directive.kinds is not None
    assert kind in directive.kinds
