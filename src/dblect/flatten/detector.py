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

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.lineage.builder import build_manifest_graph
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef
from dblect.lineage.properties.array_nonemptiness import ArrayNonEmpty, array_nonemptiness
from dblect.lineage.property import propagate, resolved_column_ref
from dblect.manifest import Manifest
from dblect.sql import Finding, detect_inner_flatten_row_drop

Detector = Callable[[Expr], tuple[Finding, ...]]


def make_array_nonemptiness_detectors(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
    column_graph: ColumnLineageGraph | None = None,
) -> tuple[Detector, ...]:
    """Curry the inner-flatten detector against propagated array non-emptiness.

    Building the graph with ``parsed`` resolves each model on a qualified copy and writes the
    resolved ``ColumnRef`` back onto those shared trees, so the detector reads an unnested
    column's identity straight off the tree it scans, through CTE and model boundaries alike.
    The property is propagated once over that graph; the non-empty ``ColumnRef``s are the set
    the predicate tests membership in. ``profile`` is the run's resolved target, fixing the
    parse dialect.

    ``column_graph`` lets the audit pass the manifest column graph it already built over the
    same ``parsed`` trees, so the heavy qualify-and-resolve walk (which is also what stamped
    those trees) runs once per audit rather than once per fact family. When it is omitted the
    factory builds its own graph over ``parsed``, the standalone posture tests use.
    """
    graph = (
        column_graph
        if column_graph is not None
        else build_manifest_graph(manifest, dialect=profile.sqlglot_dialect, parsed=parsed).graph
    )
    annotations = propagate(graph, array_nonemptiness)
    nonempty_refs = frozenset(
        ref for ref, ann in annotations.items() if ann.value is ArrayNonEmpty.NON_EMPTY
    )

    def column_is_nonempty(col: exp.Column) -> bool:
        ref: ColumnRef | None = resolved_column_ref(col)
        return ref is not None and ref in nonempty_refs

    def flatten(tree: Expr) -> tuple[Finding, ...]:
        return detect_inner_flatten_row_drop(tree, column_is_nonempty=column_is_nonempty)

    return (flatten,)
