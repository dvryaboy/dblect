"""Tests for the demo aggregation-depth property."""

from __future__ import annotations

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties import aggregation_depth


def _source(name: str) -> SourceRef:
    return SourceRef(SourceKind.SOURCE, f"source.test.raw.{name}")


def _model(uid: str) -> SourceRef:
    return SourceRef(SourceKind.MODEL, uid)


def test_single_aggregate_is_depth_one() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT SUM(t.x) AS total FROM t",
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT"}},
    )
    anns = propagate(graph, aggregation_depth)
    out = ColumnRef(_model("model.test.m"), "total")
    assert anns[out] == 1


def test_non_aggregate_passthrough_is_depth_zero() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT t.x AS y FROM t",
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT"}},
    )
    anns = propagate(graph, aggregation_depth)
    out = ColumnRef(_model("model.test.m"), "y")
    assert anns[out] == 0


def test_sum_of_sum_through_cte_is_depth_two() -> None:
    """Aggregate over a CTE that already aggregated must surface as depth 2 —
    this is the substrate-level check that CTE projections carry their own
    expression rather than being collapsed to a leaf stamp.
    """
    sql = """
        WITH r AS (SELECT SUM(t.x) AS subtotal FROM t GROUP BY t.bucket)
        SELECT SUM(r.subtotal) AS grand FROM r
    """
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT", "bucket": "STRING"}},
    )
    anns = propagate(graph, aggregation_depth)
    out = ColumnRef(_model("model.test.m"), "grand")
    assert anns[out] == 2


def test_aggregate_in_expression_does_not_double_count() -> None:
    """``SUM(t.x + t.y)`` is one aggregate: the default times-fold over the
    binary expression's children must take the max rather than sum.
    """
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT SUM(t.x + t.y) AS s FROM t",
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT", "y": "INT"}},
    )
    anns = propagate(graph, aggregation_depth)
    out = ColumnRef(_model("model.test.m"), "s")
    assert anns[out] == 1


def test_union_of_aggregated_arms_keeps_depth_one() -> None:
    """UNION ALL plus-fold under MaxSemiring is ``max``: two depth-1 arms
    combine to 1, not 2. Pins that the confluence isn't a naive sum.
    """
    sql = """
        SELECT u.s AS out FROM (
            SELECT SUM(t1.a) AS s FROM t1
            UNION ALL
            SELECT SUM(t2.b) AS s FROM t2
        ) u
    """
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={"t1": _source("t1"), "t2": _source("t2")},
        schema={"t1": {"a": "INT"}, "t2": {"b": "INT"}},
    )
    anns = propagate(graph, aggregation_depth)
    out = ColumnRef(_model("model.test.m"), "out")
    assert anns[out] == 1
