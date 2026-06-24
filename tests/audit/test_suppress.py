"""Tests for ``-- noqa`` directive parsing and matching."""

from __future__ import annotations

import pytest

from dblect.audit.suppress import (
    SuppressionDirective,
    apply,
    directive_matches,
    parse_directives,
)
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.sql import Finding, FindingKind, suppression_code


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


# --- parse_directives ---


def test_bare_noqa_is_an_all_kinds_directive() -> None:
    sql = "-- noqa\nselect 1\n"
    [d] = parse_directives(sql)
    assert d.line == 1
    assert d.kinds is None


def test_noqa_inside_a_string_literal_is_not_a_directive() -> None:
    # A `-- noqa` that lives inside a SQL string literal is data, not a comment, and
    # must not silence findings on that line.
    assert parse_directives("select '-- noqa' as label from t") == ()
    assert parse_directives('select "-- noqa: DBLECT_JOIN_FANOUT" as c from t') == ()


def test_real_comment_after_a_string_literal_is_still_a_directive() -> None:
    # The string-literal guard must not swallow a genuine trailing comment.
    [d] = parse_directives("select '-- not a comment' as label from t  -- noqa: DBLECT_JOIN_FANOUT")
    assert d.kinds == frozenset({FindingKind.JOIN_FANOUT})


def test_doubled_quote_escape_does_not_hide_a_trailing_comment() -> None:
    # `''` is an escaped quote inside the literal; the string closes at the final quote,
    # so the trailing `-- noqa` is a real comment.
    [d] = parse_directives("select 'it''s fine' as c from t  -- noqa")
    assert d.kinds is None


def test_coded_directive_resolves_to_its_kind() -> None:
    sql = "-- noqa: DBLECT_JOIN_FANOUT\n"
    [d] = parse_directives(sql)
    assert d.kinds == frozenset({FindingKind.JOIN_FANOUT})


def test_foreign_only_directive_suppresses_nothing() -> None:
    # A lint rule code dbt lint owns; we map it to nothing, so the directive has an
    # empty kind set and silences none of our findings.
    sql = "-- noqa: RF01\n"
    [d] = parse_directives(sql)
    assert d.kinds == frozenset()


def test_mixed_directive_keeps_only_our_code() -> None:
    sql = "-- noqa: RF01, DBLECT_JOIN_FANOUT\n"
    [d] = parse_directives(sql)
    assert d.kinds == frozenset({FindingKind.JOIN_FANOUT})


def test_codes_are_case_insensitive() -> None:
    sql = "select 1  -- NoQa: dblect_join_fanout\n"
    [d] = parse_directives(sql)
    assert d.kinds == frozenset({FindingKind.JOIN_FANOUT})


def test_trailing_comment_is_recognized() -> None:
    sql = "select * from t  -- noqa: DBLECT_NULL_GROUP_AFTER_OUTER_JOIN\n"
    [d] = parse_directives(sql)
    assert d.line == 1
    assert d.kinds == frozenset({FindingKind.NULL_GROUP_AFTER_OUTER_JOIN})


def test_empty_codes_after_colon_is_bare() -> None:
    sql = "-- noqa:   \nselect 1\n"
    [d] = parse_directives(sql)
    assert d.kinds is None


# --- regression: the noqa-file / noqa-fixture collision ---
#
# `(?![\w-])` after `noqa` is what keeps these from being read as a bare `-- noqa`
# that would silence everything on the line. That misread is exactly what this
# rewrite exists to prevent.


@pytest.mark.parametrize(
    "comment",
    [
        "-- noqa-file",
        "-- noqa-fixture: orphan handling is intentional",
        "-- noqa-fixture",
    ],
)
def test_noqa_lookalikes_produce_no_directive(comment: str) -> None:
    assert parse_directives(f"select 1  {comment}\n") == ()


# --- directive_matches ---


def test_directive_on_same_line_matches() -> None:
    d = SuppressionDirective(line=5, kinds=None)
    assert directive_matches(d, _finding(line_start=5))


def test_directive_on_previous_line_matches() -> None:
    d = SuppressionDirective(line=4, kinds=None)
    assert directive_matches(d, _finding(line_start=5))


def test_directive_two_lines_up_does_not_match() -> None:
    d = SuppressionDirective(line=3, kinds=None)
    assert not directive_matches(d, _finding(line_start=5))


def test_directive_within_multi_line_finding_matches() -> None:
    d = SuppressionDirective(line=6, kinds=None)
    assert directive_matches(d, _finding(line_start=5, line_end=7))


def test_coded_directive_only_silences_its_kind() -> None:
    d = SuppressionDirective(line=5, kinds=frozenset({FindingKind.NULL_GROUP_AFTER_OUTER_JOIN}))
    same_kind = _finding(line_start=5, kind=FindingKind.NULL_GROUP_AFTER_OUTER_JOIN)
    other_kind = _finding(line_start=5, kind=FindingKind.COALESCE_ON_JOIN_KEY)
    assert directive_matches(d, same_kind)
    assert not directive_matches(d, other_kind)


def test_empty_kinds_directive_silences_nothing() -> None:
    d = SuppressionDirective(line=5, kinds=frozenset())
    assert not directive_matches(d, _finding(line_start=5))


def test_finding_without_line_is_never_suppressed() -> None:
    d = SuppressionDirective(line=0, kinds=None)
    f = _finding(line_start=0, line_end=0)
    assert not directive_matches(d, f)


# --- apply ---


def test_apply_partitions_findings() -> None:
    directives = (SuppressionDirective(line=5, kinds=None),)
    findings = (
        _finding(line_start=5),  # suppressed
        _finding(line_start=10),  # active
    )
    active, suppressed = apply(findings, directives)
    assert len(active) == 1
    assert active[0].line_start == 10
    assert len(suppressed) == 1
    assert suppressed[0][1].kinds is None


def test_apply_with_no_directives_passes_everything_through() -> None:
    findings = (_finding(line_start=5),)
    active, suppressed = apply(findings, ())
    assert active == findings
    assert suppressed == ()


def test_apply_uses_first_matching_directive() -> None:
    directives = (
        SuppressionDirective(line=5, kinds=None),
        SuppressionDirective(line=5, kinds=frozenset({FindingKind.NULL_GROUP_AFTER_OUTER_JOIN})),
    )
    findings = (_finding(line_start=5),)
    _, suppressed = apply(findings, directives)
    assert suppressed[0][1].kinds is None


@pytest.mark.parametrize("kind", list(FindingKind))
def test_every_finding_kind_code_round_trips(kind: FindingKind) -> None:
    sql = f"-- noqa: {suppression_code(kind)}\n"
    [d] = parse_directives(sql)
    assert d.kinds is not None
    assert kind in d.kinds


# --- declaration-family (CheckFinding) suppression ---
#
# One directive scanner serves both families: the same `-- noqa` syntax, now matched
# against declaration-level findings that carry line provenance.


def _check_finding(
    *,
    line_start: int,
    line_end: int | None = None,
    kind: CheckFindingKind = CheckFindingKind.AGGREGATION_NOT_WELL_TYPED,
) -> CheckFinding:
    return CheckFinding(
        kind=kind,
        message="x",
        model_unique_id="model.shop.m",
        line_start=line_start,
        line_end=line_end if line_end is not None else line_start,
    )


@pytest.mark.parametrize("kind", list(CheckFindingKind))
def test_every_check_kind_code_round_trips(kind: CheckFindingKind) -> None:
    sql = f"-- noqa: {suppression_code(kind)}\n"
    [d] = parse_directives(sql)
    assert d.kinds is not None
    assert kind in d.kinds


def test_bare_noqa_silences_a_check_finding() -> None:
    d = SuppressionDirective(line=5, kinds=None)
    assert directive_matches(d, _check_finding(line_start=5))


def test_coded_directive_only_silences_its_check_kind() -> None:
    d = SuppressionDirective(line=5, kinds=frozenset({CheckFindingKind.AGGREGATION_NOT_WELL_TYPED}))
    same = _check_finding(line_start=5, kind=CheckFindingKind.AGGREGATION_NOT_WELL_TYPED)
    other = _check_finding(line_start=5, kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION)
    assert directive_matches(d, same)
    assert not directive_matches(d, other)


def test_check_finding_without_line_is_never_suppressed() -> None:
    d = SuppressionDirective(line=0, kinds=None)
    assert not directive_matches(d, _check_finding(line_start=0, line_end=0))


def test_structural_code_does_not_silence_a_check_finding() -> None:
    # A structural code is kind-specific to its family; it leaves a same-line
    # declaration finding active rather than blanket-silencing it.
    d = SuppressionDirective(line=5, kinds=frozenset({FindingKind.NULL_GROUP_AFTER_OUTER_JOIN}))
    assert not directive_matches(d, _check_finding(line_start=5))


def test_check_code_does_not_silence_a_structural_finding() -> None:
    d = SuppressionDirective(line=5, kinds=frozenset({CheckFindingKind.AGGREGATION_NOT_WELL_TYPED}))
    assert not directive_matches(d, _finding(line_start=5))


def test_apply_partitions_check_findings() -> None:
    directives = (SuppressionDirective(line=3, kinds=None),)
    findings = (
        _check_finding(line_start=3),  # suppressed
        _check_finding(line_start=9),  # active
    )
    active, suppressed = apply(findings, directives)
    assert [f.line_start for f in active] == [9]
    assert len(suppressed) == 1
    assert suppressed[0][1].kinds is None
