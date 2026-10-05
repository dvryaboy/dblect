"""Shared fixtures for the ``ROW_NUMBER`` conditional-key tests: a keyless ``events``
source, models over it, and the keys the audit derives for one of them."""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_relation_graph
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.predicate_flow import predicate_flow_property
from dblect.lineage.properties.uniqueness import (
    Key,
    activate_conditional,
    uniqueness_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import Node
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source

_DUCKDB = profile_for_adapter("duckdb")

_RAW = "source.shop.raw.events"
COLS = "c0, c1, c2"


def window_sql(fn: str = "ROW_NUMBER", partition: str = "c1") -> str:
    return f"SELECT {COLS}, {fn}() OVER (PARTITION BY {partition} ORDER BY c0) AS rn FROM events"


def model(name: str, sql: str, *deps: str) -> Node:
    return _node(
        f"model.shop.{name}",
        sql,
        raw=sql,
        name=name,
        depends_on=frozenset({_RAW, *(f"model.shop.{d}" for d in deps)}),
    )


def promoted_keys(final: str, *models: Node) -> frozenset[Key]:
    """The ``final`` model's keys as the audit derives them: propagate uniqueness and
    predicate-flow, then activate conditional keys."""
    manifest = _manifest(source(_RAW, name="events"), *models)
    graph = build_relation_graph(manifest).graph
    keys = propagate(graph, uniqueness_property(manifest, _DUCKDB))
    flow = propagate(graph, predicate_flow_property())
    return activate_conditional(keys, flow)[SourceRef(SourceKind.MODEL, f"model.shop.{final}")].keys
