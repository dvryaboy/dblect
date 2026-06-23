"""Resolution coverage counted as the lineage graph is built.

Coverage is measured over the model's output columns: a column is resolved when
the builder could follow the lineage of every reference it reads, blind when any
reference fell blind (qualify could not attach a source), and no site at all when
it reads nothing (a literal). An unexpanded ``SELECT *`` is one blind column of
unknown width. Counting output columns rather than every reference in every nested
scope keeps a deep CTE chain from inflating the denominator. These pin the counts
so coverage reporting rests on a measured number rather than an inferred one.
See ``docs/design/lineage-facts.md`` ("Coverage and degradation").
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage.builder import build_manifest_graph
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
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
    return model.resolved_columns, model.blind_columns, model.unexpanded_stars


def test_each_output_column_resolves_against_a_documented_upstream() -> None:
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


def test_a_column_built_from_many_references_counts_once() -> None:
    src = _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(amount="DECIMAL", tax="DECIMAL"),
    )
    model = _node(
        "model.shop.totalled",
        kind=ResourceType.MODEL,
        sql="SELECT amount + tax AS total FROM payments",
        columns=_cols(total="DECIMAL"),
    )
    assert _resolution(_manifest(src, model), "model.shop.totalled") == (1, 0, 0)


def _upstream_by_column(manifest: Manifest, uid: str) -> dict[str, set[tuple[str, str]]]:
    build = build_manifest_graph(manifest)
    assert build.issues == (), build.issues
    self_ref = SourceRef(kind=SourceKind.MODEL, unique_id=uid)
    out: dict[str, set[tuple[str, str]]] = {}
    for ref, edges in build.graph.edges.items():
        if ref.source == self_ref:
            out[ref.column] = {(r.source.unique_id, r.column) for r in edges}
    return out


def test_ddl_prelude_yields_the_same_lineage_as_the_bare_select() -> None:
    # Acceptance for the multi-statement split: a model compiled as
    # `<DDL prelude>; <SELECT>` resolves to exactly the lineage of the bare SELECT.
    src = _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(amount="DECIMAL", currency="VARCHAR"),
    )
    bare = _node(
        "model.shop.bare",
        kind=ResourceType.MODEL,
        sql="SELECT amount, currency FROM payments",
        columns=_cols(amount="DECIMAL", currency="VARCHAR"),
    )
    prelude = _node(
        "model.shop.prelude",
        kind=ResourceType.MODEL,
        sql=(
            "CREATE TEMPORARY FUNCTION add_one(x INT) AS (x + 1);\n"
            "SELECT amount, currency FROM payments"
        ),
        columns=_cols(amount="DECIMAL", currency="VARCHAR"),
    )
    manifest = _manifest(src, bare, prelude)
    bare_lineage = _upstream_by_column(manifest, "model.shop.bare")
    prelude_lineage = _upstream_by_column(manifest, "model.shop.prelude")
    assert prelude_lineage == bare_lineage
    assert _resolution(manifest, "model.shop.prelude") == _resolution(manifest, "model.shop.bare")


def test_inline_defined_function_call_resolves_to_its_argument_lineage() -> None:
    # A call to the inline-defined function is an opaque transform: it does not
    # crash, and the output column still carries the lineage of the column it reads.
    src = _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(amount="DECIMAL"),
    )
    model = _node(
        "model.shop.doubled",
        kind=ResourceType.MODEL,
        sql=(
            "CREATE TEMPORARY FUNCTION secret(x INT) AS (x * 2);\n"
            "SELECT secret(amount) AS doubled FROM payments"
        ),
        columns=_cols(doubled="DECIMAL"),
    )
    lineage = _upstream_by_column(_manifest(src, model), "model.shop.doubled")
    assert lineage["doubled"] == {("source.shop.raw.payments", "amount")}


def test_multi_result_script_is_an_unbuilt_coverage_miss() -> None:
    # A genuine multi-statement script cannot be reduced to one model statement, so
    # it is surfaced as a build issue rather than silently emptied or guessed at.
    src = _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(amount="DECIMAL"),
    )
    model = _node(
        "model.shop.script",
        kind=ResourceType.MODEL,
        sql="SELECT amount FROM payments;\nSELECT amount FROM payments",
        columns=_cols(amount="DECIMAL"),
    )
    build = build_manifest_graph(_manifest(src, model))
    miss = [i for i in build.issues if i.model_unique_id == "model.shop.script"]
    assert miss, "a multi-result script should be reported as a build issue"
    assert "result-producing" in miss[0].message
    # Nothing was emitted for the script: it is a miss, never analysed as if empty.
    self_ref = ColumnRef(
        source=SourceRef(kind=SourceKind.MODEL, unique_id="model.shop.script"), column="amount"
    )
    assert self_ref not in build.graph.edges


def test_cte_depth_does_not_inflate_the_denominator() -> None:
    src = _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(amount="DECIMAL"),
    )
    deep = _node(
        "model.shop.deep",
        kind=ResourceType.MODEL,
        sql=(
            "WITH a AS (SELECT amount FROM payments), "
            "b AS (SELECT amount FROM a) "
            "SELECT amount FROM b"
        ),
        columns=_cols(amount="DECIMAL"),
    )
    assert _resolution(_manifest(src, deep), "model.shop.deep") == (1, 0, 0)
