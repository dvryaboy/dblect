"""Back-mapping a compiled-SQL line span to the on-disk source template.

The walker hands detectors the model's compiled SQL, so a finding's line span is
compiled-relative. For a model written without Jinja the two line up; for a macro-
or ref-heavy model they diverge. These tests pin the contract of the back-mapper:
a verbatim passthrough line maps to its source line however the compiled output was
padded around it, and a line with no clean source origin degrades to a compiled-
relative span rather than guessing.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dblect.audit.sourcemap import SpanBasis, build_line_map


def test_identity_when_source_and_compiled_match() -> None:
    sql = "select a,\n       b\nfrom t\nwhere a > 0"
    m = build_line_map(sql, sql)
    span = m.map_span(2, 2)
    assert span.basis is SpanBasis.SOURCE
    assert (span.line_start, span.line_end) == (2, 2)


def test_macro_insertion_maps_passthrough_and_leaves_expansion_compiled() -> None:
    # `{{ totals() }}` expands to two lines, pushing `group by 1` from source line 4 to
    # compiled line 5. The passthrough line back-maps to source; a line that came out of
    # the macro has no source origin and stays compiled-relative.
    raw = "select\n  customer_id,\n  {{ totals() }}\ngroup by 1"
    compiled = "select\n  customer_id,\n  sum(amount) as total,\n  count(*) as n\ngroup by 1"
    m = build_line_map(compiled, raw)

    group_by = m.map_span(5, 5)
    assert group_by.basis is SpanBasis.SOURCE
    assert (group_by.line_start, group_by.line_end) == (4, 4)

    from_macro = m.map_span(4, 4)  # `count(*) as n`, emitted by the macro
    assert from_macro.basis is SpanBasis.COMPILED
    assert (from_macro.line_start, from_macro.line_end) == (4, 4)


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
