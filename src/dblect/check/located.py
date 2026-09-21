"""The shared walk-resolve-locate-sort shape every located check finding repeats.

A located declaration finding (a domain-type contradiction, a not-well-typed
aggregation, a join-key type mismatch) always assembles the same way: a reader
walks some structure (an annotation map, a set of coherence clears, the parsed
trees) and yields one row per finding, and this module turns each row into a
:class:`~dblect.check.findings.CheckFinding` with its file, its compiled span, and
its span back-mapped onto the model's source template. The reader is the only
part that varies by finding kind; the location and sorting are read off here so a
new located reader never re-derives them.

``annotation_or_grounded`` is the other repeated shape: a reader that consults a
property's flow map for a scope not every reader reaches (a join key that is
never itself projected) falls back to the scope's own declared grounding.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from sqlglot import Expr

if TYPE_CHECKING:
    from _typeshed import SupportsRichComparison

from dblect.audit.sourcemap import LineMap, SourceSpan, build_line_map
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.lineage.facts.model import Annotation
from dblect.lineage.graph import ColumnRef, Derivation, SourceRef
from dblect.manifest import Manifest
from dblect.sql import _sqlglot as sg

K = TypeVar("K")
S = TypeVar("S", ColumnRef, SourceRef)


def annotation_or_grounded(
    annotations: Mapping[S, Annotation[K]], ground: Callable[[S], Annotation[K]]
) -> Callable[[S], Annotation[K]]:
    """The propagated annotation where the walk reached ``scope``, falling back to
    its own declared grounding where it did not (a join key that is never itself
    projected, only compared in an ON clause). Every check reader that consults a
    property's flow map alongside its grounding needs exactly this fallback."""

    def get(scope: S) -> Annotation[K]:
        found = annotations.get(scope)
        return found if found is not None else ground(scope)

    return get


@dataclass(frozen=True, slots=True)
class LocatedRow:
    """One located finding, before its file and spans are resolved: the model it
    lands on, the node(s) to read a compiled line from (tried in order, first
    usable wins, matching :func:`_span_of`'s existing fallback), the finding's
    kind and message, and the column it names."""

    uid: str
    nodes: tuple[Derivation | None, ...]
    kind: CheckFindingKind
    message: str
    column: str | None


def locate_findings(
    manifest: Manifest,
    rows: Iterable[LocatedRow],
    *,
    line_maps: dict[str, LineMap],
    sort_key: Callable[[CheckFinding], SupportsRichComparison],
) -> list[CheckFinding]:
    """Assemble ``rows`` into located findings: each gets its model's file path,
    the compiled span read off its nodes, and that span back-mapped onto the
    model's source template, sharing ``line_maps`` across every row from the same
    model. The result is sorted by ``sort_key``, since two located readers rarely
    want the same order (one groups by column, another by line)."""
    out: list[CheckFinding] = []
    for row in rows:
        line_start, line_end = span_of(*row.nodes)
        out.append(
            CheckFinding(
                kind=row.kind,
                message=row.message,
                model_unique_id=row.uid,
                file_path=file_of(manifest, row.uid),
                column=row.column,
                line_start=line_start,
                line_end=line_end,
                source_span=source_span(manifest, row.uid, line_start, line_end, line_maps),
            )
        )
    out.sort(key=sort_key)
    return out


def file_of(manifest: Manifest, uid: str) -> str | None:
    node = manifest.nodes.get(uid)
    return node.original_file_path if node is not None else None


def source_span(
    manifest: Manifest,
    uid: str,
    line_start: int,
    line_end: int,
    cache: dict[str, LineMap],
) -> SourceSpan:
    """Back-map a compiled span onto the model's source template (see
    :mod:`dblect.audit.sourcemap`), reusing one line map per model across a run's
    findings."""
    # The "no line" sentinel has no source position; skip building the map for a
    # model whose findings are all unlocated.
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
    """The 1-indexed source-line span of the first ``nodes`` entry sqlglot stamped
    with a usable line, falling back through the rest. ``(0, 0)`` when none carry
    one, the convention a finding with no locatable line uses (never
    line-suppressible).

    The span is in the compiled SQL's line space; :func:`source_span` back-maps it
    onto ``raw_code``. A non-``Expr`` derivation (a ``UnionConfluence``) carries no
    line and is skipped like ``None``."""
    for node in nodes:
        if not isinstance(node, Expr):
            continue
        span = sg.line_range(node)
        if span is not None:
            return span
    return (0, 0)
