"""DAG construction and topology tests.

Combines targeted unit cases (cycle detection, edge validation) with PBT over
randomly-generated DAGs to verify topology invariants hold structurally rather
than only on hand-picked examples.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.manifest.dag import CycleError, Dag


def test_empty_dag_is_valid() -> None:
    dag = Dag.build(nodes=[], edges=[])
    assert dag.nodes == frozenset()
    assert dag.topological_order() == ()


def test_single_node_no_edges() -> None:
    dag = Dag.build(nodes=["a"], edges=[])
    assert dag.upstream("a") == frozenset()
    assert dag.downstream("a") == frozenset()
    assert dag.topological_order() == ("a",)


def test_linear_chain_topological_order() -> None:
    dag = Dag.build(nodes=["a", "b", "c"], edges=[("a", "b"), ("b", "c")])
    assert dag.topological_order() == ("a", "b", "c")


def test_diamond_topological_order_is_deterministic() -> None:
    # a -> b -> d
    # a -> c -> d
    dag = Dag.build(
        nodes=["a", "b", "c", "d"],
        edges=[("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
    )
    order = dag.topological_order()
    # Ties broken by node-id sort: b before c.
    assert order == ("a", "b", "c", "d")
    # Rerunning yields the same order.
    assert dag.topological_order() == order


def test_transitive_downstream() -> None:
    dag = Dag.build(
        nodes=["a", "b", "c", "d"],
        edges=[("a", "b"), ("b", "c"), ("b", "d")],
    )
    assert dag.transitive_downstream("a") == frozenset({"b", "c", "d"})
    assert dag.transitive_downstream("b") == frozenset({"c", "d"})
    assert dag.transitive_downstream("d") == frozenset()


def test_transitive_upstream() -> None:
    dag = Dag.build(
        nodes=["a", "b", "c", "d"],
        edges=[("a", "b"), ("b", "c"), ("b", "d")],
    )
    assert dag.transitive_upstream("d") == frozenset({"a", "b"})
    assert dag.transitive_upstream("a") == frozenset()


def test_unknown_node_in_edge_raises() -> None:
    with pytest.raises(ValueError, match="unknown node"):
        Dag.build(nodes=["a"], edges=[("a", "b")])


def test_query_unknown_node_raises() -> None:
    dag = Dag.build(nodes=["a"], edges=[])
    with pytest.raises(KeyError):
        dag.upstream("nope")


def test_cycle_two_nodes_raises() -> None:
    with pytest.raises(CycleError) as excinfo:
        Dag.build(nodes=["a", "b"], edges=[("a", "b"), ("b", "a")])
    # The witness cycle starts and ends with the same node.
    assert excinfo.value.cycle[0] == excinfo.value.cycle[-1]


def test_cycle_three_nodes_raises() -> None:
    with pytest.raises(CycleError):
        Dag.build(nodes=["a", "b", "c"], edges=[("a", "b"), ("b", "c"), ("c", "a")])


def test_self_loop_raises() -> None:
    with pytest.raises(CycleError):
        Dag.build(nodes=["a"], edges=[("a", "a")])


# -- PBT --


@st.composite
def acyclic_dag(draw: st.DrawFn, max_nodes: int = 12) -> Dag:
    """Generate a DAG by giving each node a topological rank and only allowing
    edges from lower-rank to higher-rank nodes. Guarantees acyclicity by
    construction."""
    n = draw(st.integers(min_value=0, max_value=max_nodes))
    nodes = [f"n{i}" for i in range(n)]
    edges: list[tuple[str, str]] = []
    if n >= 2:
        # Up to ~n^2/4 random forward edges, deduplicated.
        candidates = [(nodes[i], nodes[j]) for i in range(n) for j in range(i + 1, n)]
        max_edges = max(1, min(len(candidates), n * 2))
        chosen = draw(
            st.lists(
                st.sampled_from(candidates),
                min_size=0,
                max_size=max_edges,
                unique=True,
            )
        )
        edges = chosen
    return Dag.build(nodes=nodes, edges=edges)


@given(acyclic_dag())
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_topological_order_respects_edges(dag: Dag) -> None:
    order = dag.topological_order()
    position = {n: i for i, n in enumerate(order)}
    # Every node appears exactly once and every edge respects the order.
    assert set(order) == dag.nodes
    assert len(order) == len(dag.nodes)
    for n in dag.nodes:
        for downstream in dag.downstream(n):
            assert position[n] < position[downstream]


@given(acyclic_dag())
def test_transitive_downstream_closes_under_iteration(dag: Dag) -> None:
    for n in dag.nodes:
        direct = dag.downstream(n)
        transitive = dag.transitive_downstream(n)
        # Direct downstream is a subset of transitive downstream.
        assert direct <= transitive
        # Transitive downstream of n equals direct downstream plus all their transitives.
        recomputed = set(direct)
        for d in direct:
            recomputed |= dag.transitive_downstream(d)
        assert transitive == frozenset(recomputed)


@given(acyclic_dag())
def test_upstream_downstream_are_inverses(dag: Dag) -> None:
    for n in dag.nodes:
        for d in dag.downstream(n):
            assert n in dag.upstream(d)
        for u in dag.upstream(n):
            assert n in dag.downstream(u)
