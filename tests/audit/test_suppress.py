"""Tests for ``-- noqa-fixture:`` directive parsing and matching."""

from __future__ import annotations

import pytest

from dblect.audit.suppress import (
    SuppressionDirective,
    apply,
    directive_matches,
    parse_directives,
)
from dblect.sql import Finding, FindingKind


def _finding(
    *,
    line_start: int,
    line_end: int | None = None,
    kind: FindingKind = FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
) -> Finding:
    return Finding(
        kind=kind,
        message="x",
        sql_snippet="snippet",
        line_start=line_start,
        line_end=line_end if line_end is not None else line_start,
    )


def test_parses_directive_with_plain_reason() -> None:
    sql = "-- noqa-fixture: orphan handling is intentional\nselect 1\n"
    directives, malformed = parse_directives(sql)
    assert malformed == ()
    assert directives == (
        SuppressionDirective(line=1, kind=None, reason="orphan handling is intentional"),
    )


def test_parses_kind_specific_directive() -> None:
    sql = "-- noqa-fixture: null_group_after_outer_join: orphan handling\n"
    directives, malformed = parse_directives(sql)
    assert malformed == ()
    [d] = directives
    assert d.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN
    assert d.reason == "orphan handling"


def test_unknown_kind_falls_back_to_all_kinds() -> None:
    # A typo in the kind ("unordered_window" vs "unordered_ranking_window") is
    # safer treated as an all-kinds suppression than silently failing to suppress.
    sql = "-- noqa-fixture: unordered_window: typo here\n"
    directives, malformed = parse_directives(sql)
    assert malformed == ()
    [d] = directives
    assert d.kind is None
    assert "unordered_window" in d.reason


def test_reason_with_colon_is_not_misread_as_kind_claim() -> None:
    sql = "-- noqa-fixture: TODO: revisit Q3 2026\n"
    directives, _ = parse_directives(sql)
    [d] = directives
    assert d.kind is None
    assert d.reason == "TODO: revisit Q3 2026"


def test_bare_noqa_fixture_is_malformed() -> None:
    sql = "-- noqa-fixture\nselect 1\n"
    directives, malformed = parse_directives(sql)
    assert directives == ()
    [bad] = malformed
    assert bad.kind is FindingKind.MALFORMED_SUPPRESSION
    assert bad.line_start == 1
    assert "reason" in bad.message.lower()


def test_empty_reason_is_malformed() -> None:
    sql = "-- noqa-fixture:   \nselect 1\n"
    directives, malformed = parse_directives(sql)
    assert directives == ()
    [bad] = malformed
    assert bad.kind is FindingKind.MALFORMED_SUPPRESSION


def test_case_insensitive_token() -> None:
    sql = "-- NoQa-Fixture: works either way\n"
    directives, _ = parse_directives(sql)
    assert len(directives) == 1


def test_trailing_comment_is_recognized() -> None:
    sql = "select * from t  -- noqa-fixture: not a real issue\n"
    directives, _ = parse_directives(sql)
    [d] = directives
    assert d.line == 1
    assert d.reason == "not a real issue"


# --- directive_matches ---


def test_directive_on_same_line_matches() -> None:
    d = SuppressionDirective(line=5, kind=None, reason="r")
    assert directive_matches(d, _finding(line_start=5))


def test_directive_on_previous_line_matches() -> None:
    d = SuppressionDirective(line=4, kind=None, reason="r")
    assert directive_matches(d, _finding(line_start=5))


def test_directive_two_lines_up_does_not_match() -> None:
    d = SuppressionDirective(line=3, kind=None, reason="r")
    assert not directive_matches(d, _finding(line_start=5))


def test_directive_within_multi_line_finding_matches() -> None:
    d = SuppressionDirective(line=6, kind=None, reason="r")
    assert directive_matches(d, _finding(line_start=5, line_end=7))


def test_kind_specific_directive_only_silences_its_kind() -> None:
    d = SuppressionDirective(
        line=5,
        kind=FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
        reason="r",
    )
    same_kind = _finding(line_start=5, kind=FindingKind.NULL_GROUP_AFTER_OUTER_JOIN)
    other_kind = _finding(line_start=5, kind=FindingKind.COALESCE_ON_JOIN_KEY)
    assert directive_matches(d, same_kind)
    assert not directive_matches(d, other_kind)


def test_finding_without_line_is_never_suppressed() -> None:
    d = SuppressionDirective(line=0, kind=None, reason="r")
    f = _finding(line_start=0, line_end=0)
    assert not directive_matches(d, f)


# --- apply ---


def test_apply_partitions_findings() -> None:
    directives = (SuppressionDirective(line=5, kind=None, reason="r"),)
    findings = (
        _finding(line_start=5),  # suppressed
        _finding(line_start=10),  # active
    )
    active, suppressed = apply(findings, directives)
    assert len(active) == 1
    assert active[0].line_start == 10
    assert len(suppressed) == 1
    assert suppressed[0][1].reason == "r"


def test_apply_with_no_directives_passes_everything_through() -> None:
    findings = (_finding(line_start=5),)
    active, suppressed = apply(findings, ())
    assert active == findings
    assert suppressed == ()


def test_apply_uses_first_matching_directive() -> None:
    # Two directives could each match; the first one wins (and only one ends
    # up as the recorded reason).
    directives = (
        SuppressionDirective(line=5, kind=None, reason="first"),
        SuppressionDirective(line=5, kind=None, reason="second"),
    )
    findings = (_finding(line_start=5),)
    _, suppressed = apply(findings, directives)
    assert suppressed[0][1].reason == "first"


@pytest.mark.parametrize("kind", list(FindingKind))
def test_every_finding_kind_value_round_trips_through_kind_claim(kind: FindingKind) -> None:
    sql = f"-- noqa-fixture: {kind.value}: any reason\n"
    directives, _ = parse_directives(sql)
    [d] = directives
    # MALFORMED_SUPPRESSION as a *claimed* kind is meaningless but we don't
    # want the parser to crash on it either.
    assert d.kind is kind or kind is FindingKind.MALFORMED_SUPPRESSION
    if kind is not FindingKind.MALFORMED_SUPPRESSION:
        assert d.reason == "any reason"
