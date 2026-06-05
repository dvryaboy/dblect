"""Relation-scoped uniqueness propagation, end to end through the substrate.

These pin the relation algebra at the contract boundary: build a manifest of
sources and models, ground declared keys with the uniqueness discoverers, run the
one propagator, and read each relation's candidate-key set. The rules under test
are the sound ones a key walk can justify: a passthrough carries the source's
keys, a JOIN keeps the probe side's keys only when the joined-in side is unique on
the join columns, GROUP BY and DISTINCT introduce a key, UNION ALL keeps none, and
a declared key on a model unions with what the SQL proves.
"""

from __future__ import annotations

from collections.abc import Mapping

# The relation-graph builder lives next to the column builder.
from dblect.lineage.builder import build_relation_graph
from dblect.lineage.graph import SourceKind
from dblect.lineage.properties.uniqueness import CandidateKeySet, Key, uniqueness_property
from dblect.lineage.property import propagate
from dblect.manifest import (
    Column,
    ConstraintSpec,
    ConstraintType,
    DbtTestMetadata,
    Manifest,
    Node,
    ResourceType,
)


def _model(uid: str, sql: str, *, constraints: tuple[ConstraintSpec, ...] = (),
           columns: Mapping[str, Column] = {}) -> Node:
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
        columns=columns,
        constraints=constraints,
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


def _unique(uid: str, *, column: str, target: str) -> Node:
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
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}),
        attached_node=target,
    )


def _key(*cols: str) -> Key:
    return frozenset(cols)


def _keys(*nodes: Node) -> dict[str, CandidateKeySet]:
    """Build a manifest from the nodes, propagate uniqueness, and return each
    model's candidate-key set keyed by unique_id."""
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )
    result = build_relation_graph(manifest)
    prop = uniqueness_property(manifest, name_to_source={})
    anns = propagate(result.graph, prop)
    return {
        ref.unique_id: ann.value
        for ref, ann in anns.items()
        if ref.kind is SourceKind.MODEL
    }


def test_passthrough_carries_the_source_key() -> None:
    src = _source("source.shop.raw.orders")
    keys = _keys(
        src,
        _unique("test.shop.u", column="id", target=src.unique_id),
        _model("model.shop.stg", "SELECT id, amount FROM orders"),
    )
    assert keys["model.shop.stg"] == CandidateKeySet.of(_key("id"))


def test_projection_rename_remaps_the_key() -> None:
    src = _source("source.shop.raw.orders")
    keys = _keys(
        src,
        _unique("test.shop.u", column="id", target=src.unique_id),
        _model("model.shop.stg", "SELECT id AS order_id, amount FROM orders"),
    )
    assert keys["model.shop.stg"] == CandidateKeySet.of(_key("order_id"))


def test_group_by_introduces_a_key_on_the_group_columns() -> None:
    src = _source("source.shop.raw.orders")
    keys = _keys(
        src,
        _model(
            "model.shop.by_customer",
            "SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id",
        ),
    )
    assert keys["model.shop.by_customer"] == CandidateKeySet.of(_key("customer_id"))


def test_distinct_introduces_a_full_tuple_key() -> None:
    src = _source("source.shop.raw.orders")
    keys = _keys(
        src,
        _model("model.shop.d", "SELECT DISTINCT customer_id, region FROM orders"),
    )
    assert keys["model.shop.d"] == CandidateKeySet.of(_key("customer_id", "region"))


def test_join_preserves_probe_keys_when_joined_side_is_unique_on_the_key() -> None:
    """A LEFT JOIN to a dimension unique on the join key cannot fan out, so the
    probe side's key survives."""
    orders = _source("source.shop.raw.orders")
    customers = _source("source.shop.raw.customers")
    keys = _keys(
        orders,
        customers,
        _unique("test.shop.o", column="id", target=orders.unique_id),
        _unique("test.shop.c", column="id", target=customers.unique_id),
        _model(
            "model.shop.enriched",
            "SELECT o.id, c.region FROM orders o "
            "LEFT JOIN customers c ON o.customer_id = c.id",
        ),
    )
    assert keys["model.shop.enriched"] == CandidateKeySet.of(_key("id"))


def test_join_drops_keys_when_joined_side_is_not_unique_on_the_key() -> None:
    """The joined-in side is not known unique on the join column, so the join can
    fan out and no key is proven."""
    orders = _source("source.shop.raw.orders")
    events = _source("source.shop.raw.events")
    keys = _keys(
        orders,
        events,
        _unique("test.shop.o", column="id", target=orders.unique_id),
        _model(
            "model.shop.joined",
            "SELECT o.id, e.kind FROM orders o LEFT JOIN events e ON o.id = e.order_id",
        ),
    )
    assert keys["model.shop.joined"] == CandidateKeySet.of()  # no key proven


def test_union_all_proves_no_key() -> None:
    a = _source("source.shop.raw.a")
    b = _source("source.shop.raw.b")
    keys = _keys(
        a,
        b,
        _unique("test.shop.a", column="id", target=a.unique_id),
        _unique("test.shop.b", column="id", target=b.unique_id),
        _model("model.shop.u", "SELECT id FROM a UNION ALL SELECT id FROM b"),
    )
    assert keys["model.shop.u"] == CandidateKeySet.of()


def test_union_distinct_proves_the_full_tuple_key() -> None:
    a = _source("source.shop.raw.a")
    b = _source("source.shop.raw.b")
    keys = _keys(
        a,
        b,
        _model("model.shop.u", "SELECT id, kind FROM a UNION SELECT id, kind FROM b"),
    )
    assert keys["model.shop.u"] == CandidateKeySet.of(_key("id", "kind"))


def test_cross_model_propagation_through_a_stage() -> None:
    """Keys flow across model boundaries: the staging model carries the source key,
    and the mart that selects from the staging model carries it too."""
    src = _source("source.shop.raw.orders")
    keys = _keys(
        src,
        _unique("test.shop.u", column="id", target=src.unique_id),
        _model("model.shop.stg", "SELECT id, amount FROM orders"),
        _model("model.shop.mart", "SELECT id FROM stg"),
    )
    assert keys["model.shop.mart"] == CandidateKeySet.of(_key("id"))


def test_declared_model_key_unions_with_sql_derived_key() -> None:
    """A native PRIMARY KEY declared on the model and a DISTINCT-derived key both
    hold, so the model carries both (reconcile by meet, no conflict)."""
    src = _source("source.shop.raw.orders")
    keys = _keys(
        src,
        _model(
            "model.shop.d",
            "SELECT DISTINCT customer_id, region FROM orders",
            constraints=(ConstraintSpec(type=ConstraintType.PRIMARY_KEY, columns=("customer_id",)),),
        ),
    )
    assert keys["model.shop.d"] == CandidateKeySet.of(_key("customer_id"), _key("customer_id", "region"))
