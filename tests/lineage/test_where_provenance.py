"""Tests for the where-provenance property.

Where-provenance is the simplest non-trivial property: every output column's
annotation should be exactly the set of source columns whose values fed into
it. The builder records that set as the column's ``edges`` entry (computed by
walking sqlglot's lineage); the propagator computes it independently by
walking the projection expression. The agreement of those two paths is the
main invariant the V0 substrate proves.
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


def test_pass_through_column_traces_to_its_source() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.id FROM users u",
        name_to_source={"users": _source("users")},
        schema={"users": {"id": "INT"}},
    )
    anns = propagate(graph, where_provenance)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.test.m"), "id")
    assert anns[out] == frozenset({ColumnRef(_source("users"), "id")})


def test_transform_unions_input_columns() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.a + u.b AS sum_ab FROM t u",
        name_to_source={"t": _source("t")},
        schema={"t": {"a": "INT", "b": "INT"}},
    )
    anns = propagate(graph, where_provenance)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.test.m"), "sum_ab")
    src = _source("t")
    assert anns[out] == frozenset({ColumnRef(src, "a"), ColumnRef(src, "b")})


def test_literal_constant_has_empty_provenance() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT 42 AS the_answer FROM t",
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT"}},
    )
    anns = propagate(graph, where_provenance)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.test.m"), "the_answer")
    assert anns[out] == frozenset()


def test_aggregate_inherits_input_provenance() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT SUM(t.x) AS total FROM t",
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT"}},
    )
    anns = propagate(graph, where_provenance)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.test.m"), "total")
    assert anns[out] == frozenset({ColumnRef(_source("t"), "x")})


def test_count_star_has_empty_provenance() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT COUNT(*) AS n FROM t",
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT"}},
    )
    anns = propagate(graph, where_provenance)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.test.m"), "n")
    assert anns[out] == frozenset()


def test_join_merges_both_sides() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=(
            "SELECT a.id AS user_id, a.name, b.amount "
            "FROM users a JOIN orders b ON a.id = b.user_id"
        ),
        name_to_source={"users": _source("users"), "orders": _source("orders")},
        schema={
            "users": {"id": "INT", "name": "STRING"},
            "orders": {"user_id": "INT", "amount": "DECIMAL"},
        },
    )
    anns = propagate(graph, where_provenance)
    self_ref = SourceRef(SourceKind.MODEL, "model.test.m")
    u, o = _source("users"), _source("orders")
    assert anns[ColumnRef(self_ref, "user_id")] == frozenset({ColumnRef(u, "id")})
    assert anns[ColumnRef(self_ref, "name")] == frozenset({ColumnRef(u, "name")})
    assert anns[ColumnRef(self_ref, "amount")] == frozenset({ColumnRef(o, "amount")})


def test_cte_collapses_to_source_leaf() -> None:
    sql = """
        WITH renamed AS (SELECT x AS val FROM t)
        SELECT r.val + 1 AS bumped FROM renamed r
    """
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT"}},
    )
    anns = propagate(graph, where_provenance)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.test.m"), "bumped")
    assert anns[out] == frozenset({ColumnRef(_source("t"), "x")})


def test_propagator_agrees_with_builder_edges_on_simple_sql() -> None:
    """The propagator and the builder's recorded edges must agree on where-provenance.

    Edges are computed by the builder by walking sqlglot's lineage tree down
    to leaves. The propagator computes the same set by walking the projection
    expression and folding via the union semiring. The two paths agreeing is
    what makes the substrate trustworthy for future, more interesting
    properties (uniqueness, nullability) where the builder cannot precompute
    the answer.
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
    for col, leaves in graph.edges.items():
        # Annotations on model-output columns must equal the recorded edges.
        if col.source.kind is SourceKind.MODEL:
            assert anns[col] == leaves, f"mismatch on {col}"


@pytest.fixture(scope="module")
def jaffle_manifest(tmp_path_factory: pytest.TempPathFactory) -> Manifest:
    fixture = Path(__file__).parent.parent / "fixtures" / "jaffle" / "manifest.json"
    if not fixture.exists():
        pytest.skip("jaffle fixture not present; run scripts/refresh_jaffle_fixtures.sh")
    return Manifest.from_file(fixture)


def test_jaffle_cross_model_propagator_matches_builder_edges(jaffle_manifest: Manifest) -> None:
    """End-to-end: across the jaffle DAG, propagated annotations equal recorded edges.

    Walks every model output column in the cross-model graph and confirms the
    propagator's union-semiring annotation matches the builder's edge set.
    Any divergence here would mean either the builder's edge computation or
    the propagator's walk is wrong; both paths must agree by construction.
    """
    result = build_manifest_graph(jaffle_manifest)
    anns = propagate(result.graph, where_provenance)
    mismatches: list[str] = []
    for col, leaves in result.graph.edges.items():
        if col.source.kind is not SourceKind.MODEL:
            continue
        if anns[col] != leaves:
            mismatches.append(
                f"{col.source.unique_id}:{col.column} edges={sorted(repr(c) for c in leaves)} "
                f"annotation={sorted(repr(c) for c in anns[col])}"
            )
    assert not mismatches, "\n".join(mismatches[:5])
