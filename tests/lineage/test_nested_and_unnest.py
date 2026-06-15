"""Nested-field (STRUCT) lineage and UNNEST grain.

Two soundness concerns for warehouses with nested data (see issue #88):

* **Struct field lineage.** ``t.payload.id`` must carry lineage to the *leaf*
  field, distinct from a sibling field ``t.payload.amt``. Resolving both to the
  struct root ``payload`` would conflate two columns the analysis must keep apart.
* **UNNEST grain.** ``UNNEST(arr)`` produces one output row per array element, so
  a key unique on the parent relation is not unique on the exploded output. The
  uniqueness walk must not carry the parent key across the explosion (it may, as a
  precision refinement, recover ``(parent_key, offset)`` when an element
  discriminator is present, but it must never over-claim).
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_model_graph, build_relation_graph
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.uniqueness import (
    CandidateKeySet,
    Key,
    uniqueness_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType

_DUCKDB = profile_for_adapter("duckdb")
_T = SourceRef(SourceKind.SOURCE, "source.s.t")


# --- struct field lineage (builder) ---------------------------------------------


def _edges(sql: str, schema: Mapping[str, Mapping[str, str]]) -> dict[str, set[tuple[str, str]]]:
    graph = build_model_graph(
        model_uid="model.m",
        sql=sql,
        name_to_source={"t": _T},
        schema=schema,
        dialect="bigquery",
    )
    return {
        ref.column: {(u.source.unique_id, u.column) for u in graph.edges.get(ref, frozenset())}
        for ref in graph.subjects()
        if ref.source == SourceRef(SourceKind.MODEL, "model.m")
    }


_STRUCT_SCHEMA = {"t": {"payload": "STRUCT<id INT64, amt FLOAT64>", "id": "INT64"}}


def test_struct_field_projection_resolves_to_the_nested_leaf() -> None:
    edges = _edges(
        "SELECT t.payload.id AS a, t.payload.amt AS b FROM t",
        _STRUCT_SCHEMA,
    )
    assert edges["a"] == {("source.s.t", "payload.id")}
    assert edges["b"] == {("source.s.t", "payload.amt")}


def test_deeply_nested_struct_field_carries_the_full_path() -> None:
    schema = {"t": {"payload": "STRUCT<inner STRUCT<id INT64>>"}}
    edges = _edges("SELECT t.payload.inner.id AS a FROM t", schema)
    assert edges["a"] == {("source.s.t", "payload.inner.id")}


def test_struct_root_projection_still_resolves_to_the_struct_column() -> None:
    # Selecting the whole struct (no field access) is unchanged: the leaf is the
    # struct column itself.
    edges = _edges("SELECT t.payload AS p FROM t", _STRUCT_SCHEMA)
    assert edges["p"] == {("source.s.t", "payload")}


# --- UNNEST grain (uniqueness propagation) --------------------------------------


def _model(uid: str, sql: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
    )


def _source(uid: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.SOURCE,
        fqn=(uid,),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _unique(uid: str, *, column: str, target: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.OTHER,
        fqn=(uid,),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}),
        attached_node=target,
    )


def _keys(*nodes: Node) -> dict[str, CandidateKeySet]:
    manifest = Manifest(
        schema_version="v12", adapter_type="bigquery", nodes={n.unique_id: n for n in nodes}
    )
    graph = build_relation_graph(manifest, dialect="bigquery").graph
    anns = propagate(graph, uniqueness_property(manifest, profile_for_adapter("bigquery")))
    return {ref.unique_id: ann.value for ref, ann in anns.items() if ref.kind is SourceKind.MODEL}


def _key(*cols: str) -> Key:
    return frozenset(cols)


def test_unnest_explodes_grain_so_the_parent_key_does_not_survive() -> None:
    orders = _source("source.shop.raw.orders")
    keys = _keys(
        orders,
        _unique("test.shop.u", column="id", target=orders.unique_id),
        # one row per (order, tag): `id` is no longer unique on the output.
        _model("model.shop.order_tags", "SELECT o.id, tag FROM orders o, UNNEST(o.tags) AS tag"),
    )
    assert _key("id") not in keys["model.shop.order_tags"].keys


def test_left_join_unnest_also_explodes_grain() -> None:
    orders = _source("source.shop.raw.orders")
    keys = _keys(
        orders,
        _unique("test.shop.u", column="id", target=orders.unique_id),
        _model(
            "model.shop.order_tags",
            "SELECT o.id, tag FROM orders o LEFT JOIN UNNEST(o.tags) AS tag ON TRUE",
        ),
    )
    assert _key("id") not in keys["model.shop.order_tags"].keys
