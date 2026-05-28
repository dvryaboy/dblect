"""Demo tests for the nullability property over the immediate-upstream substrate.

Each test is one of issue #25's acceptance criteria: a scenario whose
correct annotation depends on CTE projection expressions and UNION arm
separation being visible to the propagator as first-class graph entries.
With those entries in place, the generic propagator handles each case
without any per-scenario plumbing.

``nullability`` is a demo property; see its module docstring for the gaps
that keep it out of audit consumption. The role of this file is to pin
the substrate-level behaviour these scenarios exercise.
"""

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
    """The default nullability property defaults every leaf to UNKNOWN; tests
    that want concrete NON_NULL/NULLABLE leaves swap in a fixture rule.
    """
    return Property(
        name=nullability.name,
        semiring=nullability.semiring,
        source=rule,
        operators=nullability.operators,
        aggregates=nullability.aggregates,
        unknown_value=nullability.unknown_value,
    )


def test_coalesce_inside_cte_propagates_non_null_through_outer_projection() -> None:
    """A CTE that wraps ``COALESCE(nullable, non_null)`` and an outer projection
    that references it: the outer column should be NON_NULL.

    The annotation is correct only when the CTE column ``r.safe`` is its
    own graph entry whose expression is ``Coalesce(nullable_col,
    non_null_col)``; the nullability rule then fires on the coalesce
    directly and the outer projection inherits NON_NULL through a single
    Column stamp.
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


def test_union_arm_nullability_combines_via_plus_not_times() -> None:
    """A UNION ALL with one NON_NULL arm and one NULLABLE arm: the combined
    output is NULLABLE.

    The UNION output is a synthetic graph entry whose expression is
    ``exp.Union(arm0_col, arm1_col)``. The propagator dispatches on
    ``exp.Union`` and folds arm values via ``semiring.plus``, which for
    nullability is "any nullable arm taints the output."
    """
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


def test_union_arms_both_non_null_propagate_non_null() -> None:
    """The counterpart to the previous test: when both arms are NON_NULL,
    the combined output is NON_NULL. Pins that the plus-fold isn't
    accidentally always-NULLABLE.
    """
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
    anns = propagate(graph, _with_source_rule(lambda _: Nullability.NON_NULL))
    out = ColumnRef(_model("model.test.m"), "out")
    assert anns[out] is Nullability.NON_NULL


def test_is_not_null_predicate_returns_non_null() -> None:
    """``SELECT x IS NOT NULL AS flag``: the projection is a boolean, never NULL.

    Sanity check on the operator-rule dispatch path; if this fails the
    ``exp.Is`` rule isn't wired up.
    """
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT t.x IS NOT NULL AS flag FROM t",
        name_to_source={"t": _source("t")},
        schema={"t": {"x": "INT"}},
    )
    anns = propagate(graph, _with_source_rule(lambda _: Nullability.NULLABLE))
    out = ColumnRef(_model("model.test.m"), "flag")
    assert anns[out] is Nullability.NON_NULL
