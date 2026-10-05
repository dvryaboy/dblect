"""Run the functional-dependency property over a small manifest, for propagation tests."""

from __future__ import annotations

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_relation_graph
from dblect.lineage.facts.model import Annotation, Fact
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.functional_dependency import (
    FDSet,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.properties.uniqueness import uniqueness_property
from dblect.lineage.property import propagate
from dblect.manifest import Node
from tests._manifest_builders import manifest as _manifest

_DUCKDB = profile_for_adapter("duckdb")

FdFacts = Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]]


def propagate_fds(facts: FdFacts, *nodes: Node, read_keys: bool = False) -> dict[str, FDSet]:
    """Build a manifest from the nodes, propagate the FD property (after uniqueness
    when ``read_keys`` is set, so the key-derived source is live), and return each
    model's FD set keyed by unique_id."""
    manifest = _manifest(*nodes)
    graph = build_relation_graph(manifest).graph
    ground = functional_dependency_grounding(facts)
    if read_keys:
        uniq = uniqueness_property(manifest, _DUCKDB)
        store = AnnotationStore()
        for scope, ann in propagate(graph, uniq).items():
            store.record(uniq.name, scope, ann)
        prop = functional_dependency_property(ground, uniqueness=uniq.ref)
        ctx = PropertyRegistry((uniq, prop)).dep_context(store)
        anns: Mapping[SourceRef, Annotation[FDSet]] = propagate(graph, prop, dep_context=ctx)
    else:
        anns = propagate(graph, functional_dependency_property(ground))
    return {ref.unique_id: ann.value for ref, ann in anns.items() if ref.kind is SourceKind.MODEL}
