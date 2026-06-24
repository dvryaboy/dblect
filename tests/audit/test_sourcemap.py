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


def test_passthrough_line_maps_through_a_macro_insertion() -> None:
    # Source line 3 (`group by 1`) survives verbatim into compiled, but a macro
    # expanded two lines ahead of it, pushing it to compiled line 5.
    raw = "select\n  customer_id,\n  {{ totals() }}\ngroup by 1"
    compiled = "select\n  customer_id,\n  sum(amount) as total,\n  count(*) as n\ngroup by 1"
    m = build_line_map(compiled, raw)
    span = m.map_span(5, 5)  # `group by 1` in compiled
    assert span.basis is SpanBasis.SOURCE
    assert (span.line_start, span.line_end) == (4, 4)  # `group by 1` in raw


def test_expanded_region_degrades_to_compiled() -> None:
    raw = "select\n  customer_id,\n  {{ totals() }}\ngroup by 1"
    compiled = "select\n  customer_id,\n  sum(amount) as total,\n  count(*) as n\ngroup by 1"
    m = build_line_map(compiled, raw)
    # compiled line 4 (`count(*) as n`) came out of the macro; it has no source line.
    span = m.map_span(4, 4)
    assert span.basis is SpanBasis.COMPILED
    assert (span.line_start, span.line_end) == (4, 4)


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


# --- property: verbatim passthrough lines survive arbitrary insertion ---------

_SQL_LINE = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=122),
    min_size=1,
    max_size=12,
).filter(lambda s: s.strip() != "")


@given(
    source_lines=st.lists(_SQL_LINE, min_size=1, max_size=8, unique=True),
    inserts=st.lists(st.tuples(st.integers(0, 8), _SQL_LINE), max_size=6),
)
def test_unique_passthrough_lines_map_back_to_their_source_index(
    source_lines: list[str], inserts: list[tuple[int, str]]
) -> None:
    """A line that passes through verbatim and is distinct from every other line
    maps back to the source index it came from, no matter how many macro-expanded
    lines were spliced around it. Distinctness rules out the genuine ambiguity of a
    repeated line, which the mapper is allowed to anchor anywhere it legitimately
    matches."""
    inserted_tokens = {tok for _, tok in inserts}
    # Restrict the claim to source lines that stay unambiguous after insertion.
    unambiguous = {ln for ln in source_lines if ln not in inserted_tokens}

    compiled_lines = list(source_lines)
    for at, tok in sorted(inserts, reverse=True):
        compiled_lines.insert(min(at, len(compiled_lines)), tok)

    raw = "\n".join(source_lines)
    compiled = "\n".join(compiled_lines)
    m = build_line_map(compiled, raw)

    for compiled_idx, line in enumerate(compiled_lines, start=1):
        if line not in unambiguous:
            continue
        span = m.map_span(compiled_idx, compiled_idx)
        assert span.basis is SpanBasis.SOURCE
        assert source_lines[span.line_start - 1] == line
