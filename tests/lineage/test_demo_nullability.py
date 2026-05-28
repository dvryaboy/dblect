"""Tests for the demo nullability property."""

from __future__ import annotations

from collections.abc import Callable

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties import Nullability, nullability
from dblect.lineage.property import Property


def _source(name: str) -> SourceRef:
    return SourceRef(SourceKind.SOURCE, f"source.test.raw.{name}")


def _model(uid: str) -> SourceRef:
    return SourceRef(SourceKind.MODEL, uid)


def _with_source_rule(rule: Callable[[ColumnRef], Nullability]) -> Property[Nullability]:
    return Property(
        name=nullability.name,
        semiring=nullability.semiring,
        source=rule,
        operators=nullability.operators,
        aggregates=nullability.aggregates,
        unknown_value=nullability.unknown_value,
    )


def test_coalesce_inside_cte_propagates_non_null() -> None:
    """A CTE column built from ``COALESCE(nullable, non_null)`` must propagate
    NON_NULL out through the outer projection — the rule has to fire on the
    CTE's stored expression, not a leaf-collapsed shape.
    """
    sql = """
        WITH r AS (
            SELECT COALESCE(t.maybe, t.always) AS safe
            FROM t
        )
        SELECT r.safe AS out FROM r
    """
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={"t": _source("t")},
        schema={"t": {"maybe": "INT", "always": "INT"}},
    )

    def src_rule(c: ColumnRef) -> Nullability:
        if c.column == "maybe":
            return Nullability.NULLABLE
        if c.column == "always":
            return Nullability.NON_NULL
        return Nullability.UNKNOWN

    anns = propagate(graph, _with_source_rule(src_rule))
    out = ColumnRef(_model("model.test.m"), "out")
    assert anns[out] is Nullability.NON_NULL


def test_union_arm_nullability_combines_via_plus() -> None:
    """One NON_NULL arm + one NULLABLE arm taints the combined output."""
    sql = """
        SELECT u.x AS out FROM (
            SELECT t1.a AS x FROM t1
            UNION ALL
            SELECT t2.b AS x FROM t2
        ) u
    """
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={"t1": _source("t1"), "t2": _source("t2")},
        schema={"t1": {"a": "INT"}, "t2": {"b": "INT"}},
    )

    def src_rule(c: ColumnRef) -> Nullability:
        if c.source.unique_id == "source.test.raw.t1":
            return Nullability.NON_NULL
        if c.source.unique_id == "source.test.raw.t2":
            return Nullability.NULLABLE
        return Nullability.UNKNOWN

    anns = propagate(graph, _with_source_rule(src_rule))
    out = ColumnRef(_model("model.test.m"), "out")
    assert anns[out] is Nullability.NULLABLE
