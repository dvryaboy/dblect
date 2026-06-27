"""Fact-grounded inner-flatten detector: clear an ``UNNEST`` of a provably non-empty array.

The structural :func:`~dblect.sql.patterns.detect_inner_flatten_row_drop` reads one tree
at a time and cannot see that a column array was rebuilt non-empty in an upstream model.
Here the ``array_nonemptiness`` property is propagated once over the manifest column graph,
reduced to a per-relation set of output columns proved non-empty, and threaded back into the
detector. An ``UNNEST`` of one of those columns drops no row and goes quiet; an ``UNNEST`` of
a raw source array (whose emptiness is an ingestion fact) keeps firing. This is the
opportunistic, silent-on-projects-that-declare-nothing posture the other fact-grounded
detectors take, so it needs no opt-in flag.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.lineage.builder import build_manifest_graph, index_by_name
from dblect.lineage.graph import SourceRef
from dblect.lineage.properties.array_nonemptiness import ArrayNonEmpty, array_nonemptiness
from dblect.lineage.property import propagate
from dblect.manifest import Manifest
from dblect.sql import Finding, detect_inner_flatten_row_drop

Detector = Callable[[Expr], tuple[Finding, ...]]


def make_array_nonemptiness_detectors(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
) -> tuple[Detector, ...]:
    """Curry the inner-flatten detector against propagated array non-emptiness.

    The property is propagated once over the whole-manifest column graph; ``parsed``
    shares the audit's already-parsed trees so the graph build does not re-parse.
    ``profile`` is the run's resolved target, fixing the parse dialect. The result is a
    per-relation-name map of output columns proved non-empty, which the detector uses to
    clear an ``UNNEST`` of one of them.
    """
    graph = build_manifest_graph(manifest, dialect=profile.sqlglot_dialect, parsed=parsed).graph
    annotations = propagate(graph, array_nonemptiness)
    by_source: dict[SourceRef, set[str]] = {}
    for ref, ann in annotations.items():
        if ann.value is ArrayNonEmpty.NON_EMPTY:
            by_source.setdefault(ref.source, set()).add(ref.column)
    model_nonempty = index_by_name(
        manifest, {ref: frozenset(cols) for ref, cols in by_source.items()}
    )

    def flatten(tree: Expr) -> tuple[Finding, ...]:
        return detect_inner_flatten_row_drop(tree, model_nonempty=model_nonempty)

    return (flatten,)
