"""Resolution coverage counted as the lineage graph is built.

Every projection column reference the builder meets is resolved (lineage the
propagator can follow) or blind (qualify could not attach a source); an
unexpanded ``SELECT *`` is a blind site of unknown width. These pin the counts
so coverage reporting rests on a measured number rather than an inferred one.
See ``docs/design/lineage-facts.md`` ("Coverage and degradation").
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage.builder import build_manifest_graph
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import Column


def _cols(**types: str) -> Mapping[str, Column]:
    return {n: Column(name=n, data_type=t, description=None) for n, t in types.items()}


def _node(uid: str, *, kind: ResourceType, sql: str | None, columns: Mapping[str, Column]) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=kind,
        fqn=tuple(uid.split(".")[1:]),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=f"models/{uid.split('.')[-1]}.sql",
        columns=columns,
    )


def _manifest(*nodes: Node) -> Manifest:
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def _resolution(manifest: Manifest, uid: str) -> tuple[int, int, int]:
    build = build_manifest_graph(manifest)
    [model] = [m for m in build.resolution if m.unique_id == uid]
    return model.resolved_refs, model.blind_refs, model.unexpanded_stars


def test_every_column_reference_resolves_against_a_documented_upstream() -> None:
    src = _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(amount="DECIMAL", currency="VARCHAR"),
    )
    model = _node(
        "model.shop.stg",
        kind=ResourceType.MODEL,
        sql="SELECT amount, currency FROM payments",
        columns=_cols(amount="DECIMAL", currency="VARCHAR"),
    )
    resolved, blind, stars = _resolution(_manifest(src, model), "model.shop.stg")
    assert (resolved, blind, stars) == (2, 0, 0)


def test_unexpanded_select_star_is_a_blind_site() -> None:
    # A source with no documented columns: qualify cannot expand `*`, so the
    # projection is one unexpanded-star blind site and no reference resolves.
    src = _node("source.shop.raw.opaque", kind=ResourceType.SOURCE, sql=None, columns=_cols())
    model = _node(
        "model.shop.passthru",
        kind=ResourceType.MODEL,
        sql="SELECT * FROM opaque",
        columns=_cols(x="INT"),
    )
    resolved, _blind, stars = _resolution(_manifest(src, model), "model.shop.passthru")
    assert resolved == 0
    assert stars == 1


def test_literal_only_model_has_no_resolution_sites() -> None:
    model = _node(
        "model.shop.k",
        kind=ResourceType.MODEL,
        sql="SELECT 1 AS one, 'x' AS label",
        columns=_cols(one="INT", label="VARCHAR"),
    )
    assert _resolution(_manifest(model), "model.shop.k") == (0, 0, 0)
