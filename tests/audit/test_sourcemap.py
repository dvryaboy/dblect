"""Back-mapping a compiled-SQL line span to the on-disk source template.

The walker hands detectors the model's compiled SQL, so a finding's line span is
compiled-relative. For a model written without Jinja the two line up; for a macro-
or ref-heavy model they diverge. These tests pin the contract of the back-mapper:
a verbatim passthrough line maps to its source line however the compiled output was
padded around it, and a line with no clean source origin degrades to a compiled-
relative span rather than guessing.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from dblect.audit.sourcemap import SpanBasis, build_line_map


def test_identity_when_source_and_compiled_match() -> None:
    sql = "select a,\n       b\nfrom t\nwhere a > 0"
    m = build_line_map(sql, sql)
    span = m.map_span(2, 2)
    assert span.basis is SpanBasis.SOURCE
    assert (span.line_start, span.line_end) == (2, 2)


def test_macro_insertion_maps_passthrough_and_anchors_expansion_to_its_call() -> None:
    # `{{ totals() }}` expands to two lines, pushing `group by 1` from source line 4 to
    # compiled line 5. The passthrough line back-maps to source; a line that came out of
    # the macro has no source line of its own, so it anchors to the `{{ totals() }}` call
    # site (source line 3) that the developer can place a `-- noqa` on.
    raw = "select\n  customer_id,\n  {{ totals() }}\ngroup by 1"
    compiled = "select\n  customer_id,\n  sum(amount) as total,\n  count(*) as n\ngroup by 1"
    m = build_line_map(compiled, raw)

    group_by = m.map_span(5, 5)
    assert group_by.basis is SpanBasis.SOURCE
    assert (group_by.line_start, group_by.line_end) == (4, 4)

    from_macro = m.map_span(4, 4)  # `count(*) as n`, emitted by the macro
    assert from_macro.basis is SpanBasis.MACRO_CALL
    assert (from_macro.line_start, from_macro.line_end) == (3, 3)


def test_indentation_change_still_maps() -> None:
    raw = "select a,\nb\nfrom t"
    compiled = "select a,\n    b\nfrom t"  # compiler re-indented line 2
    m = build_line_map(compiled, raw)
    span = m.map_span(2, 2)
    assert span.basis is SpanBasis.SOURCE
    assert (span.line_start, span.line_end) == (2, 2)


def test_no_raw_code_is_compiled_basis() -> None:
    m = build_line_map("select 1\nfrom t", None)
    span = m.map_span(2, 2)
    assert span.basis is SpanBasis.COMPILED
    assert (span.line_start, span.line_end) == (2, 2)


def test_zero_line_sentinel_is_preserved_as_compiled() -> None:
    sql = "select 1"
    m = build_line_map(sql, sql)
    span = m.map_span(0, 0)
    assert span.basis is SpanBasis.COMPILED
    assert (span.line_start, span.line_end) == (0, 0)


def test_contiguous_multiline_span_maps_to_source() -> None:
    # A multi-line span whose every line passes through verbatim maps as a whole: a
    # leading compiled prelude shifts source lines 2-3 down to compiled lines 3-4.
    raw = "select\n  a,\n  b\nfrom t"
    compiled = "-- prelude\nselect\n  a,\n  b\nfrom t"
    m = build_line_map(compiled, raw)
    span = m.map_span(3, 4)
    assert span.basis is SpanBasis.SOURCE
    assert (span.line_start, span.line_end) == (2, 3)


def test_multiline_span_with_macro_emitted_interior_degrades_to_compiled() -> None:
    # A span (compiled 2-4) whose endpoints anchor but whose interior line came out of a
    # macro is not a clean source region. Anchoring only the endpoints would report a
    # SOURCE span covering a source line the construct never occupied, so the mapper
    # declines to compiled-relative rather than over-claim.
    raw = "select a,\n  b,\n  c\nfrom t"
    compiled = "select a,\n  b,\n  macro_emitted,\n  c\nfrom t"
    m = build_line_map(compiled, raw)
    span = m.map_span(2, 4)
    assert span.basis is SpanBasis.COMPILED
    assert (span.line_start, span.line_end) == (2, 4)


def test_non_monotonic_span_degrades_to_compiled() -> None:
    # A span whose endpoints map to source lines out of order (start after end) is
    # not a coherent source region; the mapper declines rather than emit a reversed
    # span.
    raw = "a\nb\nc"
    # Compiled reorders the lines, so line 1 -> source 3 and line 3 -> source 1.
    compiled = "c\nb\na"
    m = build_line_map(compiled, raw)
    span = m.map_span(1, 3)
    assert span.basis is SpanBasis.COMPILED


# --- macro-call anchoring: a macro-emitted span points at its call site -------


def test_macro_emitted_span_anchors_to_its_only_call_site() -> None:
    # The macro emits two compiled lines (3-4) between the `customer_id` and `group by`
    # anchors; the only `{{ ... }}` in that source gap is `{{ totals() }}` at line 3, so
    # both emitted lines anchor there.
    raw = "select\n  customer_id,\n  {{ totals() }}\ngroup by 1"
    compiled = "select\n  customer_id,\n  sum(amount) as total,\n  count(*) as n\ngroup by 1"
    m = build_line_map(compiled, raw)
    for compiled_line in (3, 4):
        span = m.map_span(compiled_line, compiled_line)
        assert span.basis is SpanBasis.MACRO_CALL
        assert (span.line_start, span.line_end) == (3, 3)


def test_two_call_sites_in_one_gap_degrade_to_compiled() -> None:
    # Two `{{ ... }}` calls sit in the same source gap, so a macro-emitted compiled line
    # between the anchors cannot be blamed on one call over the other. The mapper declines
    # rather than guess, keeping the honest compiled line.
    raw = "select\n  {{ dims() }},\n  {{ metrics() }}\nfrom t"
    compiled = "select\n  a, b,\n  c, d\nfrom t"
    m = build_line_map(compiled, raw)
    span = m.map_span(2, 2)
    assert span.basis is SpanBasis.COMPILED
    assert (span.line_start, span.line_end) == (2, 2)


def test_macro_call_inside_for_loop_is_reachable() -> None:
    # A `{{ ... }}` bracketed by a `{% for %}` still carries its source line in the parsed
    # tree, so loop-emitted SQL anchors to the loop body the developer wrote (source 3).
    raw = "select\n{% for c in cols %}\n  {{ metric(c) }},\n{% endfor %}\n  1 as x\nfrom t"
    compiled = "select\n  sum(a) as a_m,\n  sum(b) as b_m,\n  1 as x\nfrom t"
    m = build_line_map(compiled, raw)
    for compiled_line in (2, 3):  # both loop iterations' output
        span = m.map_span(compiled_line, compiled_line)
        assert span.basis is SpanBasis.MACRO_CALL
        assert (span.line_start, span.line_end) == (3, 3)


def test_macro_call_inside_if_block_is_reachable() -> None:
    # A `{{ ... }}` bracketed by a `{% if %}` (taken branch) anchors to the call line
    # inside the conditional (source 4), not to the `{% if %}` header.
    raw = "select\n  id,\n{% if include_totals %}\n  {{ totals() }}\n{% endif %}\nfrom t"
    compiled = "select\n  id,\n  sum(amount) as total\nfrom t"
    m = build_line_map(compiled, raw)
    span = m.map_span(3, 3)
    assert span.basis is SpanBasis.MACRO_CALL
    assert (span.line_start, span.line_end) == (4, 4)


def test_fully_generated_model_anchors_to_its_only_call() -> None:
    # A model that is nothing but one macro call has no verbatim anchors at all. The gap
    # is the whole file, and with a single call site the emitted SQL still anchors to it,
    # so even a fully generated model stays suppressible at its call line.
    raw = "{{ dbt_utils.union_relations(relations) }}"
    compiled = "select a from x\nunion all\nselect a from y"
    m = build_line_map(compiled, raw)
    for compiled_line in (1, 2, 3):
        span = m.map_span(compiled_line, compiled_line)
        assert span.basis is SpanBasis.MACRO_CALL
        assert (span.line_start, span.line_end) == (1, 1)


# --- property: soundness under arbitrary macro insertion ----------------------

# Tokens carry no whitespace, so a normalized match is an exact match and the
# soundness check can compare raw line content directly.
_SQL_LINE = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=122),
    min_size=1,
    max_size=12,
)


@given(
    source_lines=st.lists(_SQL_LINE, min_size=1, max_size=8),
    inserts=st.lists(st.tuples(st.integers(0, 8), _SQL_LINE), max_size=6),
)
def test_a_mapped_line_carries_the_content_of_the_source_line_it_names(
    source_lines: list[str], inserts: list[tuple[int, str]]
) -> None:
    """Soundness: whatever the mapper maps to SOURCE, the source line it names holds the
    same text as the compiled line. It may decline to map a line (degrading to COMPILED,
    which the greedy alignment does for a line a repeated token crowded out), but it
    never points at the wrong line. This is the guarantee that lets a finding point a
    developer at a source line without misleading them."""
    compiled_lines = list(source_lines)
    for at, tok in sorted(inserts, reverse=True):
        compiled_lines.insert(min(at, len(compiled_lines)), tok)
    m = build_line_map("\n".join(compiled_lines), "\n".join(source_lines))

    for compiled_idx, line in enumerate(compiled_lines, start=1):
        span = m.map_span(compiled_idx, compiled_idx)
        if span.basis is SpanBasis.SOURCE:
            assert source_lines[span.line_start - 1] == line


@given(
    prefix=st.lists(_SQL_LINE, min_size=1, max_size=4, unique=True),
    suffix=st.lists(_SQL_LINE, min_size=1, max_size=4, unique=True),
    emitted=st.lists(_SQL_LINE, min_size=1, max_size=4, unique=True),
)
def test_macro_emitted_lines_anchor_to_the_single_call_site(
    prefix: list[str], suffix: list[str], emitted: list[str]
) -> None:
    """Soundness of the call-site anchor: one `{{ ... }}` call between verbatim anchors,
    and every line it emits anchors to that one call's source line. The literal tokens
    carry no Jinja braces, so the call line never collides with an emitted or anchor line.
    Mutually distinct lines keep difflib from anchoring an emitted line to a coincidental
    literal twin, which would (correctly) take it out of the macro-emitted population."""
    assume(len(set(prefix + suffix + emitted)) == len(prefix) + len(suffix) + len(emitted))
    call_line = len(prefix) + 1
    raw = "\n".join([*prefix, "{{ m() }}", *suffix])
    compiled = "\n".join([*prefix, *emitted, *suffix])
    m = build_line_map(compiled, raw)
    for offset in range(len(emitted)):
        span = m.map_span(len(prefix) + offset + 1, len(prefix) + offset + 1)
        assert span.basis is SpanBasis.MACRO_CALL
        assert (span.line_start, span.line_end) == (call_line, call_line)
