"""The rewritten propagator: carries Annotation, threads DepContext, reconciles
grounded against inferred into the flow value, and dispatches on scope kind.

The walk cases run on real SQL through ``build_model_graph``; the reconciliation
cases inject a grounding so grounded and inferred can be set independently. The
property under test uses the subset lattice (meet = intersection, join = union,
top = the universe, bottom = the empty set), a bona-fide bounded lattice whose
bottom is reachable, so every arm of the validation table is exercisable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest
import sqlglot.expressions as exp

from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Opacity
from dblect.lineage.facts.property import (
    DepContext,
    OperatorTransfer,
    Property,
    PropertyRef,
    column_property,
    relation_property,
)
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import ColumnRef, RelationLineageGraph, SourceKind, SourceRef
from dblect.lineage.property import propagate, run
from dblect.lineage.semiring import UnionSemiring

_UNIVERSE = frozenset({0, 1, 2, 3})
_Set = frozenset[int]


def _subset_lattice() -> Lattice[_Set]:
    return Lattice(
        meet=lambda a, b: a & b, join=lambda a, b: a | b, top=_UNIVERSE, bottom=frozenset()
    )


def _subset_prop(
    ground: Callable[[ColumnRef], Annotation[_Set]],
    *,
    operators: Mapping[type, OperatorTransfer[_Set]] | None = None,
    semiring: UnionSemiring[int] | None = None,
    reconcile_by_meet: bool = False,
) -> Property[_Set, ColumnRef]:
    return column_property(
        name="subset",
        lattice=_subset_lattice(),
        operators=operators or {},
        aggregates={},
        ground=ground,
        semiring=semiring,
        reconcile_by_meet=reconcile_by_meet,
    )


def _src(name: str) -> SourceRef:
    return SourceRef(SourceKind.SOURCE, f"source.test.raw.{name}")


def _model(uid: str = "model.test.m") -> SourceRef:
    return SourceRef(SourceKind.MODEL, uid)


def _concrete_for(values: dict[ColumnRef, _Set]) -> Callable[[ColumnRef], Annotation[_Set]]:
    """Ground the named scopes CONCRETE to their value; everything else IMPLICIT top."""

    def ground(col: ColumnRef) -> Annotation[_Set]:
        if col in values:
            return Annotation(values[col], Opacity.CONCRETE)
        return Annotation(_UNIVERSE, Opacity.IMPLICIT)

    return ground


# --- carrying Annotation through the walk ------------------------------------


def test_pass_through_carries_concrete_leaf_value() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.id FROM users u",
        name_to_source={"users": _src("users")},
        schema={"users": {"id": "INT"}},
    )
    leaf = ColumnRef(_src("users"), "id")
    out = ColumnRef(_model(), "id")
    anns = propagate(graph, _subset_prop(_concrete_for({leaf: frozenset({0})})))
    assert anns[out] == Annotation(frozenset({0}), Opacity.CONCRETE)


def test_leaf_with_no_declaration_is_implicit_top() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.id FROM users u",
        name_to_source={"users": _src("users")},
        schema={"users": {"id": "INT"}},
    )
    out = ColumnRef(_model(), "id")
    anns = propagate(graph, _subset_prop(_concrete_for({})))
    assert anns[out] == Annotation(_UNIVERSE, Opacity.IMPLICIT)


def test_literal_grounds_to_top() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT 42 AS answer FROM t",
        name_to_source={"t": _src("t")},
        schema={"t": {"x": "INT"}},
    )
    out = ColumnRef(_model(), "answer")
    anns = propagate(graph, _subset_prop(_concrete_for({})))
    assert anns[out].value == _UNIVERSE


# --- confluence --------------------------------------------------------------


def test_confluence_without_semiring_uses_lattice_join() -> None:
    """A UNION ALL of two arms folds with the lattice join (set union here)."""
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
        name_to_source={"t1": _src("t1"), "t2": _src("t2")},
        schema={"t1": {"a": "INT"}, "t2": {"b": "INT"}},
    )
    ground = _concrete_for(
        {ColumnRef(_src("t1"), "a"): frozenset({0}), ColumnRef(_src("t2"), "b"): frozenset({1})}
    )
    out = ColumnRef(_model(), "out")
    anns = propagate(graph, _subset_prop(ground))
    assert anns[out].value == frozenset({0, 1})


def test_confluence_with_semiring_uses_plus() -> None:
    """With a semiring present the confluence folds with semiring.plus. For the
    union semiring that is also set union, so the arms merge."""
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
        name_to_source={"t1": _src("t1"), "t2": _src("t2")},
        schema={"t1": {"a": "INT"}, "t2": {"b": "INT"}},
    )
    ground = _concrete_for(
        {ColumnRef(_src("t1"), "a"): frozenset({0}), ColumnRef(_src("t2"), "b"): frozenset({2})}
    )
    out = ColumnRef(_model(), "out")
    anns = propagate(graph, _subset_prop(ground, semiring=UnionSemiring[int]()))
    assert anns[out].value == frozenset({0, 2})


# --- reconciliation (grounded vs inferred -> flow) ---------------------------


def test_tightening_flows_the_more_precise_inferred_value() -> None:
    """Grounded loose, SQL proves more precise: the flow value is the inferred one."""
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.id FROM users u",
        name_to_source={"users": _src("users")},
        schema={"users": {"id": "INT"}},
    )
    leaf = ColumnRef(_src("users"), "id")
    out = ColumnRef(_model(), "id")
    ground = _concrete_for({leaf: frozenset({0}), out: frozenset({0, 1})})
    anns = propagate(graph, _subset_prop(ground))
    assert anns[out].value == frozenset({0})
    assert not anns[out].provisional


def test_conflict_keeps_grounded_and_taints_provisional() -> None:
    """SQL contradicts the grounded value: flow stays at it, marked provisional,
    rather than asserting the unsupported inferred value."""
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.id FROM users u",
        name_to_source={"users": _src("users")},
        schema={"users": {"id": "INT"}},
    )
    leaf = ColumnRef(_src("users"), "id")
    out = ColumnRef(_model(), "id")
    ground = _concrete_for({leaf: frozenset({0, 1}), out: frozenset({0})})
    anns = propagate(graph, _subset_prop(ground))
    assert anns[out].value == frozenset({0})
    assert anns[out].provisional


def test_reconcile_by_meet_composes_declared_and_inferred_without_conflict() -> None:
    """A property whose declared and inferred values are the same-polarity lower
    bounds (uniqueness: candidate keys) composes them by meet and never flags a
    conflict. Here declared {0,1} and inferred {1,2} neither refines the other, so
    the default path would taint provisional; under ``reconcile_by_meet`` the flow
    value is their meet {1}, untainted."""
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.id FROM users u",
        name_to_source={"users": _src("users")},
        schema={"users": {"id": "INT"}},
    )
    leaf = ColumnRef(_src("users"), "id")
    out = ColumnRef(_model(), "id")
    ground = _concrete_for({leaf: frozenset({1, 2}), out: frozenset({0, 1})})
    anns = propagate(graph, _subset_prop(ground, reconcile_by_meet=True))
    assert anns[out].value == frozenset({1})
    assert not anns[out].provisional


def test_opaque_inferred_keeps_grounded_without_a_taint() -> None:
    """When the SQL reveals nothing (inferred top), the grounded value stands and
    is not tainted."""
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT 42 AS id FROM t",
        name_to_source={"t": _src("t")},
        schema={"t": {"x": "INT"}},
    )
    out = ColumnRef(_model(), "id")
    ground = _concrete_for({out: frozenset({0})})
    anns = propagate(graph, _subset_prop(ground))
    assert anns[out].value == frozenset({0})
    assert not anns[out].provisional


def test_explicit_optout_short_circuits_the_walk() -> None:
    """A node grounded opaque flows top-EXPLICIT even though it has a derivation."""
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.id FROM users u",
        name_to_source={"users": _src("users")},
        schema={"users": {"id": "INT"}},
    )
    leaf = ColumnRef(_src("users"), "id")
    out = ColumnRef(_model(), "id")

    def ground(col: ColumnRef) -> Annotation[_Set]:
        if col == out:
            return Annotation(_UNIVERSE, Opacity.EXPLICIT)
        if col == leaf:
            return Annotation(frozenset({0}), Opacity.CONCRETE)
        return Annotation(_UNIVERSE, Opacity.IMPLICIT)

    anns = propagate(graph, _subset_prop(ground))
    assert anns[out] == Annotation(_UNIVERSE, Opacity.EXPLICIT)


# --- DepContext threading ----------------------------------------------------


def test_operator_transfer_receives_threaded_dep_context() -> None:
    """The dep_context handed to propagate reaches a registered operator transfer,
    and reads through it resolve against the backing store."""
    captured: list[DepContext] = []
    dep_ref: PropertyRef[_Set, ColumnRef] = _subset_prop(_concrete_for({})).ref

    def add_rule(
        _expr: object, kids: tuple[Annotation[_Set], ...], ctx: DepContext
    ) -> Annotation[_Set]:
        captured.append(ctx)
        seen = ctx.annotation(dep_ref, ColumnRef(_src("t"), "a"))
        return seen if seen is not None else Annotation(frozenset(), Opacity.CONCRETE)

    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.a + u.b AS s FROM t u",
        name_to_source={"t": _src("t")},
        schema={"t": {"a": "INT", "b": "INT"}},
    )

    store = AnnotationStore()
    store.record(
        dep_ref.name, ColumnRef(_src("t"), "a"), Annotation(frozenset({3}), Opacity.CONCRETE)
    )
    ctx = PropertyRegistry((_subset_prop(_concrete_for({})),)).dep_context(store)

    out = ColumnRef(_model(), "s")
    anns = propagate(
        graph, _subset_prop(_concrete_for({}), operators={exp.Add: add_rule}), dep_context=ctx
    )
    assert len(captured) == 1
    assert captured[0] is ctx
    assert anns[out].value == frozenset({3})


# --- scope dispatch and the registry driver ---------------------------------


def test_relation_property_without_a_reducer_cannot_propagate() -> None:
    """A relation property carries its relation-algebra walk as ``reducer``; one
    built without it has no generic fallback (unlike column scope), so the driver
    raises at its single dispatch point rather than mid-walk. The property itself
    holds the reducer, so no global state or monkeypatching is involved."""
    rel = relation_property(
        name="rel",
        lattice=_subset_lattice(),
        operators={},
        aggregates={},
        ground=lambda _s: Annotation(_UNIVERSE, Opacity.IMPLICIT),
    )
    assert rel.reducer is None
    with pytest.raises(NotImplementedError, match="reducer"):
        propagate(RelationLineageGraph.empty(), rel)


def test_run_fills_store_for_every_property() -> None:
    graph = build_model_graph(
        model_uid="model.test.m",
        sql="SELECT u.id FROM users u",
        name_to_source={"users": _src("users")},
        schema={"users": {"id": "INT"}},
    )
    leaf = ColumnRef(_src("users"), "id")
    out = ColumnRef(_model(), "id")
    p1 = column_property(
        name="p1",
        lattice=_subset_lattice(),
        operators={},
        aggregates={},
        ground=_concrete_for({leaf: frozenset({0})}),
    )
    p2 = column_property(
        name="p2",
        lattice=_subset_lattice(),
        operators={},
        aggregates={},
        ground=_concrete_for({leaf: frozenset({1})}),
    )
    store = run(graph, PropertyRegistry((p1, p2)))
    assert store.get("p1", out) == Annotation(frozenset({0}), Opacity.CONCRETE)
    assert store.get("p2", out) == Annotation(frozenset({1}), Opacity.CONCRETE)
