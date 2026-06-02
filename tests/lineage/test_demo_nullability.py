"""Tests for the demo nullability property."""

from __future__ import annotations

from collections.abc import Callable

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Annotation, Opacity
from dblect.lineage.facts.property import Property, column_property
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties import Nullability, nullability
from dblect.lineage.properties.nullability import NULLABILITY_LATTICE
from tests.lineage._lattice_laws import assert_consistency_laws, assert_lattice_laws

_nullabilities = st.sampled_from(list(Nullability))


def _source(name: str) -> SourceRef:
    return SourceRef(SourceKind.SOURCE, f"source.test.raw.{name}")


def _model(uid: str) -> SourceRef:
    return SourceRef(SourceKind.MODEL, uid)


def _with_source_rule(
    rule: Callable[[ColumnRef], Nullability],
) -> Property[Nullability, ColumnRef]:
    """The demo property with leaf values injected as REFINED declarations, so a
    source column anchors on the test's chosen nullability rather than IMPLICIT."""

    def ground(col: ColumnRef) -> Annotation[Nullability]:
        value = rule(col)
        if value is Nullability.UNKNOWN:
            return Annotation(Nullability.UNKNOWN, Opacity.IMPLICIT)
        return Annotation(value, Opacity.REFINED)

    return column_property(
        name=nullability.name,
        lattice=NULLABILITY_LATTICE,
        operators=nullability.operators,
        aggregates=nullability.aggregates,
        ground=ground,
        semiring=nullability.semiring,
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
    assert anns[out].value is Nullability.NON_NULL


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
    assert anns[out].value is Nullability.NULLABLE


@given(_nullabilities, _nullabilities, _nullabilities)
def test_nullability_lattice_laws(a: Nullability, b: Nullability, c: Nullability) -> None:
    """The nullability lattice is a bounded distributive chain, so it satisfies the
    full bounded-lattice laws (every property runs this check)."""
    assert_lattice_laws(NULLABILITY_LATTICE, a, b, c)


@given(_nullabilities, _nullabilities)
def test_nullability_consistency_laws(declared: Nullability, value: Nullability) -> None:
    assert_consistency_laws(NULLABILITY_LATTICE, declared, value)
