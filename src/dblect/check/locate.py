"""Where a finding points: the file a model lives in, the compiled-SQL span of the
construct being reported, and that span back-mapped onto the source template the
developer wrote."""

from __future__ import annotations

from sqlglot import Expr

from dblect.audit.sourcemap import LineMap, SourceSpan, build_line_map
from dblect.lineage.graph import Derivation, SourceRef
from dblect.manifest import Manifest
from dblect.sql import _sqlglot as sg


def file_of(manifest: Manifest, source: SourceRef) -> str | None:
    node = manifest.nodes.get(source.unique_id)
    return node.original_file_path if node is not None else None


def source_span(
    manifest: Manifest,
    uid: str,
    line_start: int,
    line_end: int,
    cache: dict[str, LineMap],
) -> SourceSpan:
    """Back-map a compiled span onto the model's source template (see
    :mod:`dblect.audit.sourcemap`), reusing one line map per model across the world's
    findings. The "no line" sentinel has no source position, so no map is built
    for a model whose findings are all unlocated."""
    if line_start == 0:
        return SourceSpan.compiled(line_start, line_end)
    line_map = cache.get(uid)
    if line_map is None:
        node = manifest.nodes.get(uid)
        compiled = node.analysis_sql if node is not None else None
        raw = node.raw_code if node is not None else None
        line_map = build_line_map(compiled, raw)
        cache[uid] = line_map
    return line_map.map_span(line_start, line_end)


def span_of(*nodes: Derivation | None) -> tuple[int, int]:
    """The 1-indexed compiled-SQL line span of the first ``nodes`` entry sqlglot
    stamped with a usable line, falling back through the rest; ``(0, 0)`` when none
    carry one (an unlocated finding, never line-suppressible). A non-``Expr``
    derivation (a ``UnionConfluence``) carries no line, so it is skipped like
    ``None``."""
    for node in nodes:
        if not isinstance(node, Expr):
            continue
        span = sg.line_range(node)
        if span is not None:
            return span
    return (0, 0)
