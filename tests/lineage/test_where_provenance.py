"""Tests for the where-provenance property.

``graph.edges`` records each column's immediate upstream relation;
``propagate(graph, where_provenance)`` walks each column's projection
expression and folds via the union semiring to recover the transitive
leaf closure. The two values mean different things, and these tests pin
both meanings on real SQL shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dblect.lineage import propagate
from dblect.lineage.builder import build_manifest_graph, build_model_graph
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties import where_provenance
from dblect.manifest import Manifest


def _source(name: str) -> SourceRef:
    return SourceRef(SourceKind.SOURCE, f"source.test.raw.{name}")


# Passthrough, scalar-expression union, literal, single aggregate, and JOIN /
# CTE column shapes are generated and checked against a structural ground truth
# in ``test_pbt_lineage`` (the model-based PBT). The cases kept here pin contracts
# that the generator does not reach: COUNT(*)'s empty provenance, positional UNION
# binding, the edges-vs-closure distinction, phantom-column suppression, and the
# jaffle regression.


def test_count_star_has_empty_provenance() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT COUNT(*) AS n FROM t",
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT"}},
    )
    anns = propagate(graph, where_provenance)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.test.m"), "n")
    assert anns[out].value == frozenset()


def test_union_arms_bind_positionally_not_by_alias() -> None:
    """Arms with different per-position aliases must contribute positionally:
    the standard-SQL rule is "output names come from arm 0; later arms
    contribute by position regardless of their own aliases." A name-based
    lookup would silently drop arm 1's contribution.
    """
    sql = """
        SELECT u.x AS out FROM (
            SELECT t1.a AS x FROM t1
            UNION ALL
            SELECT t2.b AS y FROM t2
        ) u
    """
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={"t1": _source("t1"), "t2": _source("t2")},
        schema={"t1": {"a": "INT"}, "t2": {"b": "INT"}},
    )
    anns = propagate(graph, where_provenance)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.test.m"), "out")
    assert anns[out].value == frozenset(
        {ColumnRef(_source("t1"), "a"), ColumnRef(_source("t2"), "b")}
    )


def test_inline_scalar_subquery_does_not_register_phantom_model_columns() -> None:
    """A scalar subquery inside a projection is an inline expression, not a
    materialised intermediate. The model's registered columns must be
    exactly the outer projection's aliases; the inner SELECT's projections
    must not surface as their own ``ColumnRef`` on the model.
    """
    sql = "SELECT a.x AS x, (SELECT MAX(b.z) FROM b) AS subq FROM a"
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={"a": _source("a"), "b": _source("b")},
        schema={"a": {"x": "INT"}, "b": {"z": "INT"}},
    )
    model = SourceRef(SourceKind.MODEL, "model.test.m")
    model_columns = {ref.column for ref in graph.expressions if ref.source == model}
    assert model_columns == {"x", "subq"}


def test_unexpanded_star_does_not_register_phantom_model_column() -> None:
    """When the source has no documented columns, ``qualify`` cannot expand
    ``SELECT *`` and the ``Star`` survives in the projection list. The
    model must not surface a ``"*"`` column for it; the correct answer is
    "we don't know what columns this model exposes."
    """
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT * FROM raw_t",
        name_to_source={"raw_t": _source("raw_t")},
        schema=None,
    )
    model = SourceRef(SourceKind.MODEL, "model.test.m")
    model_columns = {ref.column for ref in graph.expressions if ref.source == model}
    assert "*" not in model_columns


def test_edges_are_immediate_upstream_and_annotation_is_leaf_closure() -> None:
    """On a CTE-rich query, ``graph.edges`` for a model column points at a
    CTE column (one step up), the CTE column's edges point at sources, and
    ``propagate(..., where_provenance)`` resolves the model column to the
    transitive leaf closure regardless of how many CTE hops sat between.
    """
    sql = (
        "WITH a AS (SELECT id, value FROM src_a), "
        "b AS (SELECT id, label FROM src_b) "
        "SELECT a.id, a.value, b.label, a.value + 1 AS bumped "
        "FROM a JOIN b USING (id)"
    )
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={"src_a": _source("src_a"), "src_b": _source("src_b")},
        schema={
            "src_a": {"id": "INT", "value": "INT"},
            "src_b": {"id": "INT", "label": "STRING"},
        },
    )
    anns = propagate(graph, where_provenance)
    model_ref = SourceRef(SourceKind.MODEL, "model.test.m")
    cte_a = SourceRef(SourceKind.CTE, "cte.model.test.m.a")
    cte_b = SourceRef(SourceKind.CTE, "cte.model.test.m.b")
    src_a, src_b = _source("src_a"), _source("src_b")

    # Model-level columns point at CTE columns one step up.
    assert graph.edges[ColumnRef(model_ref, "id")] == frozenset({ColumnRef(cte_a, "id")})
    assert graph.edges[ColumnRef(model_ref, "value")] == frozenset({ColumnRef(cte_a, "value")})
    assert graph.edges[ColumnRef(model_ref, "label")] == frozenset({ColumnRef(cte_b, "label")})
    assert graph.edges[ColumnRef(model_ref, "bumped")] == frozenset({ColumnRef(cte_a, "value")})

    # CTE columns themselves point at source columns one step up.
    assert graph.edges[ColumnRef(cte_a, "id")] == frozenset({ColumnRef(src_a, "id")})
    assert graph.edges[ColumnRef(cte_a, "value")] == frozenset({ColumnRef(src_a, "value")})
    assert graph.edges[ColumnRef(cte_b, "id")] == frozenset({ColumnRef(src_b, "id")})
    assert graph.edges[ColumnRef(cte_b, "label")] == frozenset({ColumnRef(src_b, "label")})

    # Annotations walk the chain transitively to the leaf source.
    assert anns[ColumnRef(model_ref, "id")].value == frozenset({ColumnRef(src_a, "id")})
    assert anns[ColumnRef(model_ref, "value")].value == frozenset({ColumnRef(src_a, "value")})
    assert anns[ColumnRef(model_ref, "label")].value == frozenset({ColumnRef(src_b, "label")})
    assert anns[ColumnRef(model_ref, "bumped")].value == frozenset({ColumnRef(src_a, "value")})


@pytest.fixture(scope="module")
def jaffle_manifest(tmp_path_factory: pytest.TempPathFactory) -> Manifest:
    fixture = Path(__file__).parent.parent / "fixtures" / "jaffle" / "manifest.json"
    if not fixture.exists():
        pytest.skip("jaffle fixture not present; run scripts/refresh_jaffle_fixtures.sh")
    return Manifest.from_file(fixture)


def test_jaffle_build_succeeds_and_chains_resolve_to_real_leaves(
    jaffle_manifest: Manifest,
) -> None:
    """Regression guard on the jaffle fixture.

    * ``_build_schema`` must not collapse to an empty schema (an
      empty-column-table leak would blank the graph).
    * Every model column's where-provenance annotation must terminate at
      manifest-backed leaves. Synthetic CTE / UNION refs may appear in
      ``edges`` but never in the transitive closure — if they do, the
      propagator stopped early.
    """
    result = build_manifest_graph(jaffle_manifest)
    assert len(result.graph.edges) > 0, (
        "graph collapsed to empty; check _build_schema and BuildIssue messages"
    )
    anns = propagate(result.graph, where_provenance)
    manifest_kinds = {SourceKind.SOURCE, SourceKind.SEED, SourceKind.SNAPSHOT, SourceKind.MODEL}
    synthetic_in_annotations: list[str] = [
        f"{col.source.unique_id}:{col.column} leaks {leaf.source.kind}:{leaf.source.unique_id}"
        for col, ann in anns.items()
        if col.source.kind is SourceKind.MODEL
        for leaf in ann.value
        if leaf.source.kind not in manifest_kinds
    ]
    assert not synthetic_in_annotations, "\n".join(synthetic_in_annotations[:5])
