"""Back-map a compiled-SQL line span onto the on-disk source template.

Findings carry line numbers from the compiled SQL the parser saw. For a model
written without Jinja the compiled and source lines coincide; for a macro- or
ref-heavy model dbt's expansion pushes them apart, so a finding's span points at
the wrong place in the ``.sql`` the developer wrote.

The alignment is deliberately conservative. It anchors verbatim-passthrough lines
(whitespace collapsed, so a re-indent still matches). A line rewritten or emitted
by compilation does not match, so for those we reach for a second anchor: the
source line of the ``{{ ... }}`` call that emitted it, found in the raw template's
Jinja structure. When the gap between two verbatim anchors holds exactly one call
site the emitted span anchors there (``MACRO_CALL``); SQLFluff reaches the same
position from an instrumented render, and this reconstructs it from artifacts.
When no single source line can be found (several calls in the gap, or a fully
generated model) the span stays compiled-relative: a wrong source line reads as a
bug in the tool, so we keep the honest compiled line instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

from jinja2 import TemplateError, nodes

from dblect.templating import shared_environment

_WHITESPACE = re.compile(r"\s+")


class SpanBasis(Enum):
    """Which text a span's line numbers index.

    ``SOURCE`` indexes the on-disk template at the construct itself. ``MACRO_CALL``
    indexes it at the ``{{ ... }}`` call site that emitted the construct, which lives
    in generated SQL with no source line of its own. ``COMPILED`` indexes the compiled
    SQL the parser saw, the fallback when no source line was found.
    """

    SOURCE = "source"
    MACRO_CALL = "macro_call"
    COMPILED = "compiled"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A 1-indexed line span and the text its line numbers index."""

    line_start: int
    line_end: int
    basis: SpanBasis

    @classmethod
    def compiled(cls, line_start: int, line_end: int) -> SourceSpan:
        return cls(line_start, line_end, SpanBasis.COMPILED)


def _normalize(line: str) -> str:
    """Collapse whitespace and trim so a re-indented passthrough line still matches its
    source. A blank line normalizes to ``""`` and never anchors."""
    return _WHITESPACE.sub(" ", line.strip())


@dataclass(frozen=True, slots=True)
class LineMap:
    """Aligns compiled line indices to source line indices.

    ``_to_source[i]`` is the 0-indexed source line compiled line ``i`` maps to verbatim,
    or ``-1`` for none. ``_call_lines`` are the 1-indexed source lines holding a
    ``{{ ... }}`` construct, the candidate anchors for macro-emitted spans. ``_source_len``
    bounds the file so a call site with no verbatim anchor after it still has a gap.
    """

    _to_source: tuple[int, ...]
    _call_lines: tuple[int, ...]
    _source_len: int

    def map_span(self, line_start: int, line_end: int) -> SourceSpan:
        """Map a 1-indexed compiled span to a source span. ``line_start == 0`` is the
        detector's "no line" sentinel and has no source position."""
        if line_start <= 0 or line_end < line_start:
            return SourceSpan.compiled(line_start, line_end)
        start = self._lookup(line_start)
        if start is not None and self._anchors_verbatim(line_start, line_end, start):
            return SourceSpan(start, start + (line_end - line_start), SpanBasis.SOURCE)
        return self._macro_call_anchor(line_start, line_end)

    def _anchors_verbatim(self, line_start: int, line_end: int, start: int) -> bool:
        # Every line must map to the consecutive source run from `start`. Anchoring only
        # the endpoints would let a span whose interior a macro emitted claim source lines
        # the construct never occupied, so a gap or a jump declines to SOURCE.
        expected = start
        for compiled_line in range(line_start, line_end + 1):
            if self._lookup(compiled_line) != expected:
                return False
            expected += 1
        return True

    def _macro_call_anchor(self, line_start: int, line_end: int) -> SourceSpan:
        # The emitted lines sit between two verbatim anchors; the source lines between
        # those anchors are where the call that emitted them lives. One call site there is
        # unambiguous; none or several, we cannot tell which to blame, so keep the line.
        before = self._source_before(line_start)
        after = self._source_after(line_end)
        candidates = [c for c in self._call_lines if before < c < after]
        if len(candidates) == 1:
            return SourceSpan(candidates[0], candidates[0], SpanBasis.MACRO_CALL)
        return SourceSpan.compiled(line_start, line_end)

    def _source_before(self, line_start: int) -> int:
        """Source line of the nearest verbatim anchor before ``line_start``, else ``0``."""
        for compiled_line in range(line_start - 1, 0, -1):
            source = self._lookup(compiled_line)
            if source is not None:
                return source
        return 0

    def _source_after(self, line_end: int) -> int:
        """Source line of the nearest verbatim anchor after ``line_end``, else one past
        the file."""
        for compiled_line in range(line_end + 1, len(self._to_source) + 1):
            source = self._lookup(compiled_line)
            if source is not None:
                return source
        return self._source_len + 1

    def _lookup(self, one_indexed: int) -> int | None:
        idx = one_indexed - 1
        if 0 <= idx < len(self._to_source):
            mapped = self._to_source[idx]
            if mapped >= 0:
                return mapped + 1
        return None


def build_line_map(compiled: str | None, raw: str | None) -> LineMap:
    """Align ``compiled`` SQL to its ``raw`` template line-for-line.

    With either side absent there is nothing to anchor against, so the map is empty.
    ``autojunk`` is off so a line repeated across the model (common in generated SQL)
    still anchors on its longest matching run rather than being dropped as filler.
    """
    if not compiled or not raw:
        return LineMap((), (), 0)
    compiled_lines = compiled.splitlines()
    raw_lines = raw.splitlines()
    compiled_norm = [_normalize(line) for line in compiled_lines]
    raw_norm = [_normalize(line) for line in raw_lines]
    to_source = [-1] * len(compiled_lines)
    matcher = SequenceMatcher(a=compiled_norm, b=raw_norm, autojunk=False)
    for compiled_i, raw_i, length in matcher.get_matching_blocks():
        for offset in range(length):
            # A blank line matching a blank line is a coincidence, not an alignment.
            if compiled_norm[compiled_i + offset]:
                to_source[compiled_i + offset] = raw_i + offset
    return LineMap(tuple(to_source), _templated_call_lines(raw), len(raw_lines))


def _templated_call_lines(raw: str) -> tuple[int, ...]:
    """The 1-indexed source lines of ``raw`` that hold a ``{{ ... }}`` construct, read
    from the parsed Jinja tree (no render). An unparseable template yields none."""
    try:
        template = shared_environment().parse(raw)
    except TemplateError:
        return ()
    # Jinja coalesces a run of output into one `Output` node whose `lineno` is the start
    # of the run, so the per-call line lives on each non-literal child, not the Output.
    lines = {
        child.lineno
        for output in template.find_all(nodes.Output)
        for child in output.nodes
        if child.lineno and not isinstance(child, nodes.TemplateData)
    }
    return tuple(sorted(lines))
