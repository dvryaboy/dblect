"""Activation of conditional uniqueness facts against the predicate flow.

A ``where``-filtered ``unique`` test grounds a *conditional* key: captured, carried,
but never counted as an unconditional key until a scope's accumulated row filter
implies the test's predicate. These pin that promotion at the propagation boundary:
build a manifest, propagate uniqueness and predicate-flow, run activation, and read
each relation's keys. A relation that defines its own filter and carries a matching
conditional ``unique`` gains the key; one whose filter does not imply the predicate
keeps the key conditional (carried, not promoted).
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage.builder import build_relation_graph
from dblect.lineage.graph import SourceKind
from dblect.lineage.properties.predicate_flow import predicate_flow_property
from dblect.lineage.properties.uniqueness import (
    CandidateKeySet,
    Key,
    activate_conditional,
    uniqueness_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType
from dblect.sql import FindingKind, parse_sql
from dblect.uniqueness.detector import make_fact_grounded_detectors


def _model(uid: str, sql: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
        constraints=(),
    )


def _source(uid: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.SOURCE,
        fqn=(uid,),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _unique(uid: str, *, column: str, target: str, where: str | None = None) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.OTHER,
        fqn=(uid,),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}, where=where),
        attached_node=target,
    )


def _key(*cols: str) -> Key:
    return frozenset(cols)


def _activated(*nodes: Node) -> Mapping[str, CandidateKeySet]:
    """Propagate uniqueness and predicate-flow, activate, and return each model's
    candidate-key set by unique_id."""
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )
    graph = build_relation_graph(manifest).graph
    keys = propagate(graph, uniqueness_property(manifest))
    flow = propagate(graph, predicate_flow_property())
    activated = activate_conditional(keys, flow)
    return {ref.unique_id: cks for ref, cks in activated.items() if ref.kind is SourceKind.MODEL}


def test_conditional_key_activates_when_the_model_carries_the_filter() -> None:
    res = _activated(
        _source("source.shop.raw.orders"),
        _model("model.shop.dim", "SELECT * FROM orders WHERE active"),
        _unique("test.shop.u", column="id", target="model.shop.dim", where="active"),
    )
    assert _key("id") in res["model.shop.dim"].keys


def test_conditional_key_activates_under_a_narrower_filter() -> None:
    # The model filters ``amount > 5``, which implies the test's ``amount > 0``.
    res = _activated(
        _source("source.shop.raw.orders"),
        _model("model.shop.dim", "SELECT * FROM orders WHERE amount > 5"),
        _unique("test.shop.u", column="id", target="model.shop.dim", where="amount > 0"),
    )
    assert _key("id") in res["model.shop.dim"].keys


def test_conditional_key_activates_when_a_cte_carries_the_filter() -> None:
    # The filter sits in a CTE, not the outer WHERE. Flow accumulates through CTEs
    # for free, so the model's flow still implies the predicate.
    res = _activated(
        _source("source.shop.raw.orders"),
        _model(
            "model.shop.dim",
            "WITH a AS (SELECT * FROM orders WHERE active) SELECT * FROM a",
        ),
        _unique("test.shop.u", column="id", target="model.shop.dim", where="active"),
    )
    assert _key("id") in res["model.shop.dim"].keys


def test_conditional_key_activates_when_an_inline_subquery_carries_the_filter() -> None:
    res = _activated(
        _source("source.shop.raw.orders"),
        _model(
            "model.shop.dim",
            "SELECT * FROM (SELECT * FROM orders WHERE active) s",
        ),
        _unique("test.shop.u", column="id", target="model.shop.dim", where="active"),
    )
    assert _key("id") in res["model.shop.dim"].keys


def test_conditional_key_stays_conditional_without_a_matching_filter() -> None:
    res = _activated(
        _source("source.shop.raw.orders"),
        _model("model.shop.dim", "SELECT * FROM orders"),
        _unique("test.shop.u", column="id", target="model.shop.dim", where="active"),
    )
    dim = res["model.shop.dim"]
    assert _key("id") not in dim.keys
    assert any(ck.key == _key("id") for ck in dim.conditional)


def test_filter_that_does_not_imply_the_predicate_does_not_activate() -> None:
    # ``amount > 0`` does not imply ``amount > 5``: a wider filter is not enough.
    res = _activated(
        _source("source.shop.raw.orders"),
        _model("model.shop.dim", "SELECT * FROM orders WHERE amount > 0"),
        _unique("test.shop.u", column="id", target="model.shop.dim", where="amount > 5"),
    )
    assert _key("id") not in res["model.shop.dim"].keys


def test_unconditional_unique_still_grounds_a_key() -> None:
    # The conditional-carrying grounding must not disturb the ordinary path.
    res = _activated(
        _source("source.shop.raw.orders"),
        _model("model.shop.dim", "SELECT * FROM orders"),
        _unique("test.shop.u", column="id", target="model.shop.dim"),
    )
    assert res["model.shop.dim"].keys == frozenset({_key("id")})


# --- end to end: activation changes what the detectors see -----------------------

# A consumer joins ``dim`` on ``id``. ``dim`` declares an unconditional key on
# ``region`` and a conditional key on ``id`` (where active). The join is covered only
# when the ``id`` key is in force, so the join-fanout finding stands or falls on
# whether activation promoted it.
_CONSUMER = "SELECT f.x FROM events f JOIN dim d ON f.did = d.id"


def _fanout_kinds(*nodes: Node) -> list[FindingKind]:
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )
    _window, fanout = make_fact_grounded_detectors(manifest)
    return [f.kind for f in fanout(parse_sql(_CONSUMER, dialect="duckdb"))]


def test_activation_covers_a_join_and_suppresses_the_fanout_finding() -> None:
    # ``dim`` filters to ``active``, so its conditional ``id`` key activates and the
    # join on ``id`` is covered: no fanout.
    kinds = _fanout_kinds(
        _source("source.shop.raw.events"),
        _model("model.shop.dim", "SELECT * FROM events WHERE active"),
        _unique("test.shop.region", column="region", target="model.shop.dim"),
        _unique("test.shop.id", column="id", target="model.shop.dim", where="active"),
    )
    assert FindingKind.JOIN_FANOUT not in kinds


def test_without_the_filter_the_join_fanout_finding_stands() -> None:
    # Same shape, but ``dim`` carries no filter, so the ``id`` key stays conditional;
    # the only key is ``region``, which does not cover the join, and the fanout fires.
    kinds = _fanout_kinds(
        _source("source.shop.raw.events"),
        _model("model.shop.dim", "SELECT * FROM events"),
        _unique("test.shop.region", column="region", target="model.shop.dim"),
        _unique("test.shop.id", column="id", target="model.shop.dim", where="active"),
    )
    assert FindingKind.JOIN_FANOUT in kinds
