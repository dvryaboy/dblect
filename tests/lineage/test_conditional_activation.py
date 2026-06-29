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

from dblect.adapters import profile_for_adapter
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

_DUCKDB = profile_for_adapter("duckdb")


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
    keys = propagate(graph, uniqueness_property(manifest, _DUCKDB))
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


# --- cross-model: the conditional fact lives upstream of the filter --------------


def test_conditional_key_on_an_upstream_activates_at_a_filtering_consumer() -> None:
    # The test is on the source; a downstream model applies the implying filter. The
    # conditional key travels the walk and activates at the consumer.
    res = _activated(
        _source("source.shop.raw.orders"),
        _unique("test.shop.u", column="id", target="source.shop.raw.orders", where="active"),
        _model("model.shop.dim", "SELECT * FROM orders WHERE active"),
    )
    assert _key("id") in res["model.shop.dim"].keys


def test_cross_model_activation_renames_predicate_columns() -> None:
    # The consumer renames ``region`` to ``r``; the carried predicate renames with it,
    # matching the flow (which renames the same way), so activation still fires.
    res = _activated(
        _source("source.shop.raw.orders"),
        _unique("test.shop.u", column="id", target="source.shop.raw.orders", where="region = 'US'"),
        _model("model.shop.dim", "SELECT id, region AS r FROM orders WHERE region = 'US'"),
    )
    assert _key("id") in res["model.shop.dim"].keys


def test_cross_model_conditional_is_carried_but_not_activated_without_a_filter() -> None:
    res = _activated(
        _source("source.shop.raw.orders"),
        _unique("test.shop.u", column="id", target="source.shop.raw.orders", where="active"),
        _model("model.shop.dim", "SELECT * FROM orders"),
    )
    dim = res["model.shop.dim"]
    assert _key("id") not in dim.keys
    assert any(ck.key == _key("id") for ck in dim.conditional)


def test_conditional_carries_through_a_passthrough_chain_then_activates() -> None:
    # The fact is on the source, a staging model is a plain passthrough, and the mart
    # applies the filter. The conditional key rides through staging and activates.
    res = _activated(
        _source("source.shop.raw.orders"),
        _unique("test.shop.u", column="id", target="source.shop.raw.orders", where="active"),
        _model("model.shop.stg", "SELECT * FROM orders"),
        _model("model.shop.mart", "SELECT * FROM stg WHERE active"),
    )
    assert _key("id") not in res["model.shop.stg"].keys  # staging adds no filter
    assert _key("id") in res["model.shop.mart"].keys


def test_conditional_carries_through_a_cte_consuming_an_upstream() -> None:
    # The fact is on the source; a model filters it inside a CTE. Flow accumulates the
    # CTE filter and the conditional key rides through the CTE, so it activates.
    res = _activated(
        _source("source.shop.raw.orders"),
        _unique("test.shop.u", column="id", target="source.shop.raw.orders", where="active"),
        _model(
            "model.shop.dim",
            "WITH c AS (SELECT * FROM orders WHERE active) SELECT * FROM c",
        ),
    )
    assert _key("id") in res["model.shop.dim"].keys


def test_conditional_dropped_when_a_predicate_column_is_not_projected() -> None:
    # ``region`` (the predicate column) is filtered but not projected, so neither the
    # carried predicate nor the flow can express it: the key stays unactivated and is
    # not even carried (its predicate could not be tracked).
    res = _activated(
        _source("source.shop.raw.orders"),
        _unique("test.shop.u", column="id", target="source.shop.raw.orders", where="region = 'US'"),
        _model("model.shop.dim", "SELECT id FROM orders WHERE region = 'US'"),
    )
    dim = res["model.shop.dim"]
    assert _key("id") not in dim.keys
    assert not any(ck.key == _key("id") for ck in dim.conditional)


# --- cross-model through a join --------------------------------------------------
#
# A conditional key on the probe side rides through a *non-multiplying* join (the
# joined-in side is unique on the join columns) under an explicit projection, so a
# downstream filter still activates it. A fanning-out join, or a star over a join
# (which could blur a predicate column across the two sources), drops it.


def _join_then_filter(join_sql: str, *, lk_unique: bool) -> Mapping[str, CandidateKeySet]:
    nodes = [
        _source("source.shop.raw.orders"),
        _unique("test.shop.u", column="id", target="source.shop.raw.orders", where="region = 'US'"),
        _source("source.shop.raw.lk"),
        _model("model.shop.j", join_sql),
        _model("model.shop.m", "SELECT * FROM j WHERE region = 'US'"),
    ]
    if lk_unique:
        nodes.append(_unique("test.shop.lk", column="lk_id", target="source.shop.raw.lk"))
    return _activated(*nodes)


def test_conditional_carries_through_a_non_multiplying_join_then_activates() -> None:
    # ``lk`` is unique on its join column, so the join cannot multiply ``orders``
    # rows; the conditional ``id`` key (and its ``region`` predicate) ride through the
    # explicit projection and activate at the filtering consumer.
    res = _join_then_filter(
        "SELECT o.id, o.region, o.lk_id FROM orders o JOIN lk ON o.lk_id = lk.lk_id",
        lk_unique=True,
    )
    assert _key("id") in res["model.shop.m"].keys


def test_conditional_dropped_through_a_fanning_out_join() -> None:
    # ``lk`` has no key, so the join can multiply ``orders`` rows: the conditional key
    # cannot be trusted to survive and never reaches the consumer.
    res = _join_then_filter(
        "SELECT o.id, o.region, o.lk_id FROM orders o JOIN lk ON o.lk_id = lk.lk_id",
        lk_unique=False,
    )
    assert _key("id") not in res["model.shop.m"].keys


def test_conditional_dropped_by_a_star_over_a_join() -> None:
    # The join is non-multiplying, but ``SELECT *`` over two sources could blur the
    # predicate column across them, so the conditional key drops rather than risk it.
    res = _join_then_filter(
        "SELECT * FROM orders o JOIN lk ON o.lk_id = lk.lk_id",
        lk_unique=True,
    )
    assert _key("id") not in res["model.shop.m"].keys


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
    _window, fanout, _limit, _agg = make_fact_grounded_detectors(manifest, _DUCKDB)
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


def test_cross_model_activation_suppresses_a_join_fanout_finding() -> None:
    # The conditional ``id`` test now lives on the *source*; ``dim`` filters to
    # ``active`` and so activates it cross-model. The join on ``id`` is covered.
    kinds = _fanout_kinds(
        _source("source.shop.raw.events"),
        _unique("test.shop.id", column="id", target="source.shop.raw.events", where="active"),
        _model("model.shop.dim", "SELECT * FROM events WHERE active"),
        _unique("test.shop.region", column="region", target="model.shop.dim"),
    )
    assert FindingKind.JOIN_FANOUT not in kinds


def test_cross_model_without_the_filter_the_fanout_finding_stands() -> None:
    # Source carries the conditional ``id`` test, but ``dim`` applies no filter, so it
    # never activates; only ``region`` is a key, and the join on ``id`` fans out.
    kinds = _fanout_kinds(
        _source("source.shop.raw.events"),
        _unique("test.shop.id", column="id", target="source.shop.raw.events", where="active"),
        _model("model.shop.dim", "SELECT * FROM events"),
        _unique("test.shop.region", column="region", target="model.shop.dim"),
    )
    assert FindingKind.JOIN_FANOUT in kinds


# --- intra-model scopes: a window over a CTE that filters an upstream -------------
#
# The conditional ``id`` key is on the source; ``region`` is an unconditional key, so
# a window partitioned by ``id`` is not covered unless ``id`` activates. The CTE that
# feeds the window is where the filter is applied, so activation has to happen at that
# *intra-model* scope, not just at the model's own boundary.


def _window_kinds(model_sql: str, *nodes: Node) -> list[FindingKind]:
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )
    window_keys, _fanout, _limit, _agg = make_fact_grounded_detectors(manifest, _DUCKDB)
    return [f.kind for f in window_keys(parse_sql(model_sql, dialect="duckdb"))]


def test_intra_model_cte_activation_covers_a_window() -> None:
    # ``events`` has an unconditional ``region`` key (which does not cover the window)
    # and a conditional ``id`` key. The CTE filters to ``active``, so ``id`` activates
    # at the CTE scope and the window partitioned by ``id`` is covered.
    sql = (
        "WITH c AS (SELECT * FROM events WHERE active) "
        "SELECT row_number() OVER (PARTITION BY id ORDER BY ts) AS rn FROM c"
    )
    kinds = _window_kinds(
        sql,
        _source("source.shop.raw.events"),
        _unique("test.shop.region", column="region", target="source.shop.raw.events"),
        _unique("test.shop.id", column="id", target="source.shop.raw.events", where="active"),
        _model("model.shop.win", sql),
    )
    assert FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS not in kinds


def test_intra_model_cte_without_filter_leaves_the_window_uncovered() -> None:
    # Same shape, but the CTE applies no filter, so ``id`` never activates: only
    # ``region`` is known, it does not cover the partition, and the window is flagged.
    sql = (
        "WITH c AS (SELECT * FROM events) "
        "SELECT row_number() OVER (PARTITION BY id ORDER BY ts) AS rn FROM c"
    )
    kinds = _window_kinds(
        sql,
        _source("source.shop.raw.events"),
        _unique("test.shop.region", column="region", target="source.shop.raw.events"),
        _unique("test.shop.id", column="id", target="source.shop.raw.events", where="active"),
        _model("model.shop.win", sql),
    )
    assert FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS in kinds
