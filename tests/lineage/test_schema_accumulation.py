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
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_SEED = SourceRef(SourceKind.SEED, "seed.shop.raw")
_UP = SourceRef(SourceKind.MODEL, "model.shop.up")
_MART = SourceRef(SourceKind.MODEL, "model.shop.mart")


def _relation(
    ref: SourceRef,
    *,
    sql: str | None,
    columns: Mapping[str, Column] = {},
    depends_on: frozenset[str] = frozenset(),
    name: str | None = None,
) -> Node:
    kind = ResourceType.SEED if ref.kind is SourceKind.SEED else ResourceType.MODEL
    return _node(ref.unique_id, sql, kind=kind, name=name, columns=columns, depends_on=depends_on)


def _chain_manifest() -> Manifest:
    nodes = [
        _relation(_SEED, sql=None, columns=_cols("a", "b")),
        _relation(_UP, sql="select * from raw", depends_on=frozenset({_SEED.unique_id})),
        _relation(_MART, sql="select a from up", depends_on=frozenset({_UP.unique_id})),
    ]
    return _manifest(*nodes)


def test_downstream_resolves_columns_of_an_undocumented_upstream_model() -> None:
    build = build_manifest_graph(_chain_manifest())
    assert build.issues == ()

    # `up` selected `*` from the documented seed, so its columns are derived...
    assert ColumnRef(_UP, "a") in build.graph.edges
    # ...and the mart resolves `a` against `up` even though `up` documents nothing.
    assert build.graph.edges[ColumnRef(_MART, "a")] == frozenset({ColumnRef(_UP, "a")})


def _manifest_documented_but_unproduced() -> Manifest:
    # `up` documents `extra`, but its SQL never produces it. The running schema must keep
    # the documented column alongside the SQL-derived ones, so a dependent can resolve it.
    # This is the case a naive "replace the table with only the produced outputs" fold drops.
    nodes = [
        _relation(_SEED, sql=None, columns=_cols("a", "b")),
        _relation(
            _UP,
            sql="select a, b from raw",
            columns=_cols("a", "b", "extra"),
            depends_on=frozenset({_SEED.unique_id}),
        ),
        _relation(_MART, sql="select extra from up", depends_on=frozenset({_UP.unique_id})),
    ]
    return _manifest(*nodes)


def test_documented_column_survives_the_output_fold() -> None:
    build = build_manifest_graph(_manifest_documented_but_unproduced())
    assert build.issues == ()
    # `up`'s documented `extra` is retained even though its SQL did not produce it, so the
    # mart resolves `extra` against `up`.
    assert build.graph.edges[ColumnRef(_MART, "extra")] == frozenset({ColumnRef(_UP, "extra")})


def test_a_model_whose_name_breaks_the_schema_mirror_degrades_to_an_issue() -> None:
    # Mirroring a model's outputs into the running schema parses the relation name; a dotted
    # name parses to more parts than the depth-1 schema and raises. That must degrade the one
    # model to a BuildIssue, not abort the build and blank every other model's lineage.
    good = _relation(_UP, sql="select a, b from raw", columns=_cols("a", "b"))
    dotted = _relation(
        _MART, sql="select a from up", depends_on=frozenset({_UP.unique_id}), name="stg.orders"
    )
    manifest = _manifest(good, dotted)

    build = build_manifest_graph(manifest)  # must not raise
    assert any(issue.model_unique_id == _MART.unique_id for issue in build.issues)
    assert ColumnRef(_UP, "a") in build.graph.edges  # the well-formed upstream still built
