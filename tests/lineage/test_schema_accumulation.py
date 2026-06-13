"""``build_manifest_graph`` derives a model's columns from its SQL as it walks.

A project documents its sources and seeds (the DAG leaves have no SQL to read),
and the builder works out every model's columns from its own query in topological
order. So a model that selects ``*`` from an undocumented upstream model, or
references one of its columns by name, still resolves: by the time the builder
reaches it, the upstream model's output columns are already in the running schema.
This is what lets dblect analyse a real project without a column-by-column
schema.yml for every staging model.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage.builder import build_manifest_graph
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import Column

_SEED = SourceRef(SourceKind.SEED, "seed.shop.raw")
_UP = SourceRef(SourceKind.MODEL, "model.shop.up")
_MART = SourceRef(SourceKind.MODEL, "model.shop.mart")


def _cols(*names: str) -> Mapping[str, Column]:
    return {n: Column(name=n, data_type="VARCHAR", description=None) for n in names}


def _node(
    ref: SourceRef,
    *,
    sql: str | None,
    columns: Mapping[str, Column] = {},
    depends_on: frozenset[str] = frozenset(),
) -> Node:
    kind = ResourceType.SEED if ref.kind is SourceKind.SEED else ResourceType.MODEL
    return Node(
        unique_id=ref.unique_id,
        name=ref.unique_id.split(".")[-1],
        resource_type=kind,
        fqn=("shop", ref.unique_id.split(".")[-1]),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns=columns,
        depends_on=depends_on,
    )


def _manifest() -> Manifest:
    nodes = [
        _node(_SEED, sql=None, columns=_cols("a", "b")),
        _node(_UP, sql="select * from raw", depends_on=frozenset({_SEED.unique_id})),
        _node(_MART, sql="select a from up", depends_on=frozenset({_UP.unique_id})),
    ]
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def test_downstream_resolves_columns_of_an_undocumented_upstream_model() -> None:
    build = build_manifest_graph(_manifest())
    assert build.issues == ()

    # `up` selected `*` from the documented seed, so its columns are derived...
    assert ColumnRef(_UP, "a") in build.graph.edges
    # ...and the mart resolves `a` against `up` even though `up` documents nothing.
    assert build.graph.edges[ColumnRef(_MART, "a")] == frozenset({ColumnRef(_UP, "a")})
