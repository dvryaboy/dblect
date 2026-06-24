"""Back-map a compiled-SQL line span onto the on-disk source template.

Findings carry line numbers from the compiled SQL the parser saw. For a model
written without Jinja the compiled and source lines coincide; for a macro- or
ref-heavy model dbt's expansion pushes them apart, so a finding's span points at
the wrong place in the ``.sql`` the developer wrote. This module aligns the two
line-for-line by content and, for a compiled span, returns the matching source
span when it can anchor one and a compiled-relative span when it cannot.

The alignment is deliberately conservative. It anchors only on lines that pass
through verbatim (compared with surrounding whitespace collapsed, so a re-indent
still matches), and declines wherever a construct originates inside an expanded
macro with no clean source line. A line rewritten by compilation (``{{ ref(...) }}``
becoming a relation name) does not match and degrades to compiled-relative rather
than being mapped to a guessed source line. The audit's silent-when-unsure posture
applies: a wrong source line reads as a bug in the tool, so we keep the honest
compiled line instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

_WHITESPACE = re.compile(r"\s+")


class SpanBasis(Enum):
    """Which text a span's line numbers index.

    ``SOURCE`` lines index the on-disk template (back-mapping succeeded).
    ``COMPILED`` lines index the compiled SQL the parser saw (no clean source
    origin was found, so the compiled line is reported as the honest fallback).
    """

    SOURCE = "source"
    COMPILED = "compiled"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A 1-indexed line span and the text its line numbers index."""

    line_start: int
    line_end: int
    basis: SpanBasis


def _normalize(line: str) -> str:
    """Collapse runs of whitespace and trim, so a re-indented passthrough line still
    matches its source. Returns ``""`` for a blank line, which never anchors."""
    return _WHITESPACE.sub(" ", line.strip())


@dataclass(frozen=True, slots=True)
class LineMap:
    """Aligns compiled line indices to source line indices.

    ``_to_source[i]`` is the 0-indexed source line that compiled line ``i`` (also
    0-indexed) maps to, or ``-1`` when that compiled line has no source anchor.
    """

    _to_source: tuple[int, ...]

    def map_span(self, line_start: int, line_end: int) -> SourceSpan:
        """Map a 1-indexed compiled span to a source span when both endpoints anchor
        to source lines in order, else return the compiled span as the fallback.

        ``line_start == 0`` is the detector's "no line" sentinel; it has no source
        position by construction and stays compiled-relative."""
        start = self._lookup(line_start)
        end = self._lookup(line_end)
        if start is not None and end is not None and end >= start:
            return SourceSpan(start, end, SpanBasis.SOURCE)
        return SourceSpan(line_start, line_end, SpanBasis.COMPILED)

    def _lookup(self, one_indexed: int) -> int | None:
        """The 1-indexed source line a 1-indexed compiled line maps to, or None."""
        idx = one_indexed - 1
        if 0 <= idx < len(self._to_source):
            mapped = self._to_source[idx]
            if mapped >= 0:
                return mapped + 1
        return None


def build_line_map(compiled: str | None, raw: str | None) -> LineMap:
    """Align ``compiled`` SQL to its ``raw`` template line-for-line.

    With either side absent there is nothing to anchor against, so the map is empty
    and every span degrades to compiled-relative. ``autojunk`` is off so that a line
    repeated across the model (common in generated SQL) still anchors on its longest
    matching run rather than being dropped as popular filler.
    """
    if not compiled or not raw:
        return LineMap(())
    compiled_lines = compiled.splitlines()
    raw_lines = raw.splitlines()
    compiled_norm = [_normalize(line) for line in compiled_lines]
    raw_norm = [_normalize(line) for line in raw_lines]
    to_source = [-1] * len(compiled_lines)
    matcher = SequenceMatcher(a=compiled_norm, b=raw_norm, autojunk=False)
    for compiled_i, raw_i, length in matcher.get_matching_blocks():
        for offset in range(length):
            # A blank line matching a blank line is a coincidence, not an alignment;
            # leaving it unanchored keeps a span off lines it has no business on.
            if compiled_norm[compiled_i + offset]:
                to_source[compiled_i + offset] = raw_i + offset
    return LineMap(tuple(to_source))
