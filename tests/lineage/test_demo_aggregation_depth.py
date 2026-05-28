"""Demo tests for the aggregation-depth property over the immediate-upstream substrate.

The SUM-of-SUM-through-a-CTE scenario in issue #25's acceptance criteria
is what this file exists to pin. The CTE column is a graph entry whose
expression is ``Sum(Column(t.x))``, so the outer reference inherits
depth 1 through the Column stamp and the outer ``SUM(r.subtotal)`` ticks
the depth to 2. A detector that flags double aggregation reads
``aggregation_depth > 1`` per model column.

``aggregation_depth`` is a demo property; see its module docstring for
the gaps that keep it out of audit consumption.
"""

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
    """Baseline: ``SUM(t.x)`` directly in the model SELECT is depth 1."""
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
    """Baseline: a plain projection is depth 0."""
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
    """Aggregate over a CTE that already aggregated: depth 2.

    A ``double_aggregation`` detector built on this property checks
    ``depth > 1`` per model column. The CTE column carries its own
    ``Sum(...)`` expression as a graph entry, so the outer reference
    inherits depth 1 through a single Column stamp and the outer ``SUM``
    ticks the depth to 2.
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
    """``SUM(t.x + t.y)`` is one aggregate over a binary expression: depth 1.

    The default times-fold over the binary expression's children must
    take the max (both 0) rather than summing, so the aggregate-rule's
    ``child_k + 1`` returns 1 rather than 2.
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
    """``UNION ALL`` of two depth-1 arms: combined output is depth 1, not 2.

    Plus-fold via ``MaxSemiring.plus`` is ``max``, so two arms at depth 1
    combine to depth 1. A naive sum-style combine would have reported 2
    and produced false positives on every UNION of aggregates.
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
