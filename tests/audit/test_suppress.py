"""Tests for ``-- noqa`` directive parsing and matching."""

from __future__ import annotations

import pytest

from dblect.audit.sourcemap import SourceSpan, SpanBasis
from dblect.audit.suppress import (
    FramedDirectives,
    Suppressible,
    SuppressionDirective,
    apply,
    parse_directives,
)
from dblect.audit.walker import LocatedFinding
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.sql import Finding, FindingKind, suppression_code


def _finding(
    *,
    line_start: int,
    line_end: int | None = None,
    kind: FindingKind = FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
) -> LocatedFinding:
    # Suppression matches a finding's source-space ``located_span``, so the unit under
    # test is the located finding. The line is given in source space here (a SOURCE-basis
    # span), which is what a `-- noqa` the developer wrote is matched against.
    end = line_end if line_end is not None else line_start
    return LocatedFinding(
        model_unique_id="model.shop.m",
        file_path="models/m.sql",
        finding=Finding(
            kind=kind,
            message="x",
            sql_snippet="snippet",
            line_start=line_start,
            line_end=end,
        ),
        source_span=SourceSpan(line_start, end, SpanBasis.SOURCE) if line_start > 0 else None,
    )


# --- parse_directives ---


def test_bare_noqa_is_an_all_kinds_directive() -> None:
    sql = "-- noqa\nselect 1\n"
    [d] = parse_directives(sql)
    assert d.line == 1
    assert d.kinds is None


def test_noqa_inside_a_string_literal_is_not_a_directive() -> None:
    # `-- noqa` inside a string literal is data, not a comment.
    assert parse_directives("select '-- noqa' as label from t") == ()
    assert parse_directives('select "-- noqa: DBLECT_JOIN_FANOUT" as c from t') == ()


@pytest.mark.parametrize(
    "sql",
    [
        "select '-- not a comment' as label from t  -- noqa",
        # `''` escapes a quote inside the literal, so the string closes and the comment is real.
        "select 'it''s fine' as c from t  -- noqa",
    ],
)
def test_quote_tracking_keeps_a_real_trailing_comment(sql: str) -> None:
    [d] = parse_directives(sql)
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


# --- apply ---


def _source_frame(*directives: SuppressionDirective) -> FramedDirectives:
    """Directives placed in the source frame, where a `-- noqa` the developer wrote in
    the model template lives. A SOURCE/MACRO_CALL-basis finding is matched against these."""
    return FramedDirectives(source=directives, compiled=())


def _compiled_frame(*directives: SuppressionDirective) -> FramedDirectives:
    """Directives placed in the compiled frame, where a macro body's `-- noqa` renders
    adjacent to the construct it guards. A COMPILED-basis finding is matched against these."""
    return FramedDirectives(source=(), compiled=directives)


def _suppressed(finding: Suppressible, framed: FramedDirectives) -> bool:
    """Whether `apply` silences a single `finding`, the public boundary the admission and
    kind contracts below are pinned through."""
    _, hidden = apply((finding,), framed)
    return len(hidden) == 1


def _macro_emitted_finding(
    *,
    line_start: int,
    line_end: int | None = None,
    kind: FindingKind = FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
) -> LocatedFinding:
    # A construct emitted inside a macro body has no source line of its own, so the
    # back-map declines and its located_span stays compiled-relative (COMPILED basis).
    # The line is a compiled line, the space a compiled-frame directive is matched in.
    end = line_end if line_end is not None else line_start
    return LocatedFinding(
        model_unique_id="model.shop.m",
        file_path="models/m.sql",
        finding=Finding(
            kind=kind,
            message="x",
            sql_snippet="snippet",
            line_start=line_start,
            line_end=end,
        ),
        source_span=SourceSpan(line_start, end, SpanBasis.COMPILED),
    )


def _macro_call_finding(
    *,
    call_line: int,
    compiled_line: int,
    kind: FindingKind = FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
) -> LocatedFinding:
    # A macro-emitted construct the back-map anchored to a single `{{ ... }}` call site.
    # It occupies two real coordinates: the call line in the template (its located span,
    # MACRO_CALL basis) and the emitted line in the compiled SQL (its compiled span), so a
    # `-- noqa` on either the call line or in the macro body silences it.
    return LocatedFinding(
        model_unique_id="model.shop.m",
        file_path="models/m.sql",
        finding=Finding(
            kind=kind,
            message="x",
            sql_snippet="snippet",
            line_start=compiled_line,
            line_end=compiled_line,
        ),
        source_span=SourceSpan(call_line, call_line, SpanBasis.MACRO_CALL),
    )


def test_apply_partitions_findings() -> None:
    framed = _source_frame(SuppressionDirective(line=5, kinds=None))
    findings = (
        _finding(line_start=5),  # suppressed
        _finding(line_start=10),  # active
    )
    active, suppressed = apply(findings, framed)
    assert len(active) == 1
    assert active[0].finding.line_start == 10
    assert len(suppressed) == 1
    assert suppressed[0][1].kinds is None


def test_apply_with_no_directives_passes_everything_through() -> None:
    findings = (_finding(line_start=5),)
    active, suppressed = apply(findings, FramedDirectives(source=(), compiled=()))
    assert active == findings
    assert suppressed == ()


def test_apply_uses_first_matching_directive() -> None:
    framed = _source_frame(
        SuppressionDirective(line=5, kinds=None),
        SuppressionDirective(line=5, kinds=frozenset({FindingKind.NULL_GROUP_AFTER_OUTER_JOIN})),
    )
    findings = (_finding(line_start=5),)
    _, suppressed = apply(findings, framed)
    assert suppressed[0][1].kinds is None


# --- admission: the line window, the kind contract, and cross-family isolation ---
#
# A directive admits a finding when it sits on the line immediately above the finding's span
# or anywhere within it, and (when coded) only for the kinds it names. The window and kind
# logic is frame-agnostic, so one frame exercises it for both.


@pytest.mark.parametrize(
    ("directive_line", "expected"),
    [
        (4, True),  # the line immediately above the finding
        (5, True),  # the finding's own line
        (3, False),  # two lines above is outside the window
        (6, False),  # below a single-line finding is outside the window
    ],
)
def test_directive_admits_only_within_its_line_window(directive_line: int, expected: bool) -> None:
    framed = _source_frame(SuppressionDirective(line=directive_line, kinds=None))
    assert _suppressed(_finding(line_start=5), framed) is expected


def test_directive_within_a_multi_line_finding_matches() -> None:
    framed = _source_frame(SuppressionDirective(line=6, kinds=None))
    assert _suppressed(_finding(line_start=5, line_end=7), framed)


def test_coded_directive_only_silences_its_kind() -> None:
    framed = _source_frame(
        SuppressionDirective(line=5, kinds=frozenset({FindingKind.NULL_GROUP_AFTER_OUTER_JOIN}))
    )
    assert _suppressed(_finding(line_start=5), framed)
    assert not _suppressed(_finding(line_start=5, kind=FindingKind.COALESCE_ON_JOIN_KEY), framed)


def test_empty_kinds_directive_silences_nothing() -> None:
    framed = _source_frame(SuppressionDirective(line=5, kinds=frozenset()))
    assert not _suppressed(_finding(line_start=5), framed)


def test_finding_without_a_line_is_never_suppressed() -> None:
    # A finding with no line range can't be responsibly located, so no directive reaches it
    # in either frame.
    everywhere = FramedDirectives(
        source=(SuppressionDirective(line=1, kinds=None),),
        compiled=(SuppressionDirective(line=1, kinds=None),),
    )
    assert not _suppressed(_finding(line_start=0, line_end=0), everywhere)


# --- frame routing: a finding is matched in each frame it genuinely occupies ---
#
# A source-written `-- noqa` lives in `raw_code` and silences a finding whose located span
# the back-map placed on a source line. A macro body's `-- noqa` lives only in the compiled
# SQL, adjacent to the construct it emitted, and silences the finding on its compiled span.
# A macro-emitted finding anchored to a single call site occupies both coordinates, so
# either directive reaches it. Matching each frame against the span that indexes it keeps a
# source directive from silencing a compiled-relative finding by line-number coincidence.


def test_compiled_basis_finding_is_silenced_by_a_compiled_frame_directive() -> None:
    # The macro-body suppression path: the construct stayed compiled-relative, and a
    # directive on its compiled line silences it.
    framed = _compiled_frame(SuppressionDirective(line=5, kinds=None))
    active, suppressed = apply((_macro_emitted_finding(line_start=5),), framed)
    assert active == ()
    assert len(suppressed) == 1


def test_compiled_basis_finding_ignores_a_source_frame_directive() -> None:
    # The coordinate-frame guard: a directive in `raw_code` shares a line number with a
    # compiled-relative finding only by accident, so it must not silence it.
    framed = _source_frame(SuppressionDirective(line=5, kinds=None))
    active, suppressed = apply((_macro_emitted_finding(line_start=5),), framed)
    assert len(active) == 1
    assert suppressed == ()


def test_source_basis_finding_ignores_a_compiled_frame_directive() -> None:
    # The mirror guard: a back-mapped finding is the author's to suppress in the template,
    # so a stray compiled-space directive on the same line number does not reach it.
    framed = _compiled_frame(SuppressionDirective(line=5, kinds=None))
    active, suppressed = apply((_finding(line_start=5),), framed)
    assert len(active) == 1
    assert suppressed == ()


def test_macro_call_finding_is_silenced_by_a_macro_body_directive() -> None:
    # The headline fix: a macro emitted the construct and its guarding `-- noqa` together,
    # so the directive rides into the compiled SQL on the construct's line even though the
    # template shows only the `{{ ... }}` call. A single call site is the common shape, and
    # the macro-body directive must still reach it.
    framed = _compiled_frame(SuppressionDirective(line=5, kinds=None))
    active, suppressed = apply((_macro_call_finding(call_line=3, compiled_line=5),), framed)
    assert active == ()
    assert len(suppressed) == 1
    assert suppressed[0][2] is True  # matched in the compiled frame


def test_macro_call_finding_is_silenced_by_a_call_line_directive() -> None:
    # The template-side path: a `-- noqa` the developer wrote on the macro call line
    # silences the emitted finding, reported as a source-frame match.
    framed = _source_frame(SuppressionDirective(line=3, kinds=None))
    active, suppressed = apply((_macro_call_finding(call_line=3, compiled_line=5),), framed)
    assert active == ()
    assert len(suppressed) == 1
    assert suppressed[0][2] is False  # matched in the source frame


def test_macro_call_finding_ignores_a_source_directive_on_the_compiled_line() -> None:
    # Coincidence guard: a source directive sitting at the construct's *compiled* line
    # number must not reach a finding whose source coordinate is the call line.
    framed = _source_frame(SuppressionDirective(line=5, kinds=None))
    active, suppressed = apply((_macro_call_finding(call_line=3, compiled_line=5),), framed)
    assert len(active) == 1
    assert suppressed == ()


def test_framed_parse_splits_directives_by_text() -> None:
    framed = FramedDirectives.parse(
        raw="select 1\n-- noqa: DBLECT_JOIN_FANOUT\n",
        compiled="select 1\nselect 2\n-- noqa\n",
    )
    assert [d.line for d in framed.source] == [2]
    assert [d.line for d in framed.compiled] == [3]


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


def test_coded_directive_only_silences_its_check_kind() -> None:
    # A check finding carries no back-mapped source span, so it routes to the compiled frame.
    framed = _compiled_frame(
        SuppressionDirective(line=5, kinds=frozenset({CheckFindingKind.AGGREGATION_NOT_WELL_TYPED}))
    )
    assert _suppressed(_check_finding(line_start=5), framed)
    assert not _suppressed(
        _check_finding(line_start=5, kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION), framed
    )


def test_structural_code_does_not_silence_a_check_finding() -> None:
    # A structural code is kind-specific to its family; it leaves a same-line declaration
    # finding active rather than blanket-silencing it.
    framed = _compiled_frame(
        SuppressionDirective(line=5, kinds=frozenset({FindingKind.NULL_GROUP_AFTER_OUTER_JOIN}))
    )
    assert not _suppressed(_check_finding(line_start=5), framed)


def test_check_code_does_not_silence_a_structural_finding() -> None:
    framed = _source_frame(
        SuppressionDirective(line=5, kinds=frozenset({CheckFindingKind.AGGREGATION_NOT_WELL_TYPED}))
    )
    assert not _suppressed(_finding(line_start=5), framed)


def test_apply_partitions_check_findings() -> None:
    # `_check_finding` carries no back-mapped source span, so its located_span is
    # compiled-relative and it routes to the compiled frame.
    framed = _compiled_frame(SuppressionDirective(line=3, kinds=None))
    findings = (
        _check_finding(line_start=3),  # suppressed
        _check_finding(line_start=9),  # active
    )
    active, suppressed = apply(findings, framed)
    assert [f.line_start for f in active] == [9]
    assert len(suppressed) == 1
    assert suppressed[0][1].kinds is None
