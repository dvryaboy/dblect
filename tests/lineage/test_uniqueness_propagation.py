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

import sqlglot.expressions as exp

# The relation-graph builder lives next to the column builder.
from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_relation_graph
from dblect.lineage.graph import SourceKind
from dblect.lineage.properties.uniqueness import (
    CandidateKeySet,
    Key,
    relation_scope_keys,
    uniqueness_property,
)
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
from dblect.sql import parse_sql

_DUCKDB = profile_for_adapter("duckdb")


def _model(
    uid: str,
    sql: str,
    *,
    constraints: tuple[ConstraintSpec, ...] = (),
    columns: Mapping[str, Column] = {},
) -> Node:
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


def _leaf(uid: str, *, kind: ResourceType) -> Node:
    """A seed or snapshot leaf: a downstream model refs it by name like a source."""
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=kind,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
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
    prop = uniqueness_property(manifest, _DUCKDB)
    anns = propagate(result.graph, prop)
    return {ref.unique_id: ann.value for ref, ann in anns.items() if ref.kind is SourceKind.MODEL}


def test_passthrough_carries_the_source_key() -> None:
    src = _source("source.shop.raw.orders")
    keys = _keys(
        src,
        _unique("test.shop.u", column="id", target=src.unique_id),
        _model("model.shop.stg", "SELECT id, amount FROM orders"),
    )
    assert keys["model.shop.stg"] == CandidateKeySet.of(_key("id"))


def test_unique_test_on_a_seed_flows_into_a_model_that_refs_it() -> None:
    seed = _leaf("seed.shop.country_codes", kind=ResourceType.SEED)
    keys = _keys(
        seed,
        _unique("test.shop.u", column="code", target=seed.unique_id),
        _model("model.shop.stg_countries", "SELECT code, name FROM country_codes"),
    )
    assert keys["model.shop.stg_countries"] == CandidateKeySet.of(_key("code"))


def test_unique_test_on_a_snapshot_flows_into_a_model_that_refs_it() -> None:
    snap = _leaf("snapshot.shop.orders_snapshot", kind=ResourceType.SNAPSHOT)
    keys = _keys(
        snap,
        _unique("test.shop.u", column="dbt_scd_id", target=snap.unique_id),
        _model("model.shop.current_orders", "SELECT dbt_scd_id, amount FROM orders_snapshot"),
    )
    assert keys["model.shop.current_orders"] == CandidateKeySet.of(_key("dbt_scd_id"))


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
            "SELECT o.id, c.region FROM orders o LEFT JOIN customers c ON o.customer_id = c.id",
        ),
    )
    assert keys["model.shop.enriched"] == CandidateKeySet.of(_key("id"))


def test_right_join_does_not_preserve_probe_keys() -> None:
    """A RIGHT JOIN NULL-pads the probe (left) side on joined-in rows with no match, so the
    probe key can repeat as NULL and does not survive, even when the joined-in side is unique
    on the join column."""
    orders = _source("source.shop.raw.orders")
    customers = _source("source.shop.raw.customers")
    keys = _keys(
        orders,
        customers,
        _unique("test.shop.o", column="id", target=orders.unique_id),
        _unique("test.shop.c", column="id", target=customers.unique_id),
        _model(
            "model.shop.enriched",
            "SELECT o.id, c.region FROM orders o RIGHT JOIN customers c ON o.customer_id = c.id",
        ),
    )
    assert keys["model.shop.enriched"] == CandidateKeySet.of()


def test_full_join_does_not_preserve_probe_keys() -> None:
    """A FULL JOIN NULL-pads both sides on unmatched rows, so neither side's key identifies
    the result and no key survives."""
    orders = _source("source.shop.raw.orders")
    customers = _source("source.shop.raw.customers")
    keys = _keys(
        orders,
        customers,
        _unique("test.shop.o", column="id", target=orders.unique_id),
        _unique("test.shop.c", column="id", target=customers.unique_id),
        _model(
            "model.shop.enriched",
            "SELECT o.id, c.region FROM orders o FULL JOIN customers c ON o.customer_id = c.id",
        ),
    )
    assert keys["model.shop.enriched"] == CandidateKeySet.of()


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


# An anti-join keeps a probe row only when it has no match, and a semi-join only when it does;
# either way each is row-removing and never multiplies the probe, so the probe's keys carry
# through unconditionally, whether or not the matched side has a key of its own. The three tests
# below reuse the exact setup of ``test_join_drops_keys_when_joined_side_is_not_unique_on_the_key``
# (probe ``orders`` unique on ``id``, keyless matched ``events``): the ordinary LEFT join there
# fans out and drops the key, so the only difference here is the row-removing join, which is what
# rescues the key.


def test_anti_join_preserves_probe_keys_against_a_keyless_matched_side() -> None:
    orders = _source("source.shop.raw.orders")
    events = _source("source.shop.raw.events")
    keys = _keys(
        orders,
        events,
        _unique("test.shop.o", column="id", target=orders.unique_id),
        _model(
            "model.shop.anti",
            "SELECT o.id FROM orders o ANTI JOIN events e ON o.id = e.order_id",
        ),
    )
    assert keys["model.shop.anti"] == CandidateKeySet.of(_key("id"))


def test_semi_join_preserves_probe_keys_against_a_keyless_matched_side() -> None:
    orders = _source("source.shop.raw.orders")
    events = _source("source.shop.raw.events")
    keys = _keys(
        orders,
        events,
        _unique("test.shop.o", column="id", target=orders.unique_id),
        _model(
            "model.shop.semi",
            "SELECT o.id FROM orders o SEMI JOIN events e ON o.id = e.order_id",
        ),
    )
    assert keys["model.shop.semi"] == CandidateKeySet.of(_key("id"))


def test_left_join_is_null_anti_idiom_preserves_probe_keys() -> None:
    """The ``LEFT JOIN ... WHERE <matched join key> IS NULL`` idiom keeps only the unmatched
    probe rows, one per probe row, so the probe key survives though the bare LEFT join to the
    same keyless side (the foil above) fans out and loses it."""
    orders = _source("source.shop.raw.orders")
    events = _source("source.shop.raw.events")
    keys = _keys(
        orders,
        events,
        _unique("test.shop.o", column="id", target=orders.unique_id),
        _model(
            "model.shop.antinull",
            "SELECT o.id FROM orders o LEFT JOIN events e ON o.id = e.order_id "
            "WHERE e.order_id IS NULL",
        ),
    )
    assert keys["model.shop.antinull"] == CandidateKeySet.of(_key("id"))


def test_inner_join_carries_joined_in_key_when_probe_is_unique_on_the_join_cols() -> None:
    """The symmetric rule. ``orders`` is unique on the join column, so each ``order_items`` row
    matches at most one order and the result is keyed at the line grain. The probe key
    ``order_id`` does not survive (an order spans many items), so the joined-in ``item_id`` is
    the proven key."""
    orders = _source("source.shop.raw.orders")
    items = _source("source.shop.raw.order_items")
    keys = _keys(
        orders,
        items,
        _unique("test.shop.o", column="order_id", target=orders.unique_id),
        _unique("test.shop.i", column="item_id", target=items.unique_id),
        _model(
            "model.shop.lines",
            "SELECT o.order_id, i.item_id FROM orders o "
            "JOIN order_items i ON o.order_id = i.order_id",
        ),
    )
    assert keys["model.shop.lines"] == CandidateKeySet.of(_key("item_id"))


def test_left_join_does_not_carry_the_joined_in_key() -> None:
    """An outer join NULL-pads the joined-in columns on unmatched probe rows, so ``item_id``
    no longer identifies them and the rule must not fire."""
    orders = _source("source.shop.raw.orders")
    items = _source("source.shop.raw.order_items")
    keys = _keys(
        orders,
        items,
        _unique("test.shop.o", column="order_id", target=orders.unique_id),
        _unique("test.shop.i", column="item_id", target=items.unique_id),
        _model(
            "model.shop.lines",
            "SELECT o.order_id, i.item_id FROM orders o "
            "LEFT JOIN order_items i ON o.order_id = i.order_id",
        ),
    )
    assert keys["model.shop.lines"] == CandidateKeySet.of()


def test_inner_join_drops_joined_in_key_when_probe_not_unique_on_the_join_cols() -> None:
    """Without a known key on ``orders`` the probe is not proven unique on the join column, so
    a single item could match several orders and ``item_id`` is not a proven key."""
    orders = _source("source.shop.raw.orders")
    items = _source("source.shop.raw.order_items")
    keys = _keys(
        orders,
        items,
        _unique("test.shop.i", column="item_id", target=items.unique_id),
        _model(
            "model.shop.lines",
            "SELECT o.order_id, i.item_id FROM orders o "
            "JOIN order_items i ON o.order_id = i.order_id",
        ),
    )
    assert keys["model.shop.lines"] == CandidateKeySet.of()


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


def test_passthrough_through_a_cte_carries_the_source_key() -> None:
    src = _source("source.shop.raw.orders")
    keys = _keys(
        src,
        _unique("test.shop.u", column="id", target=src.unique_id),
        _model(
            "model.shop.stg",
            "WITH s AS (SELECT id, amount FROM orders) SELECT id FROM s",
        ),
    )
    assert keys["model.shop.stg"] == CandidateKeySet.of(_key("id"))


def test_relation_scope_keys_exposes_cte_intermediate_keys() -> None:
    """The detector-facing per-scope index carries a CTE body's keys, resolving
    base tables by name against the per-model keys propagation produced."""
    tree = parse_sql("WITH s AS (SELECT id, amount FROM orders) SELECT id FROM s")
    model_keys = {"orders": frozenset({_key("id")})}
    scopes = relation_scope_keys(tree, model_keys)
    cte_body = next(c.this for c in tree.find_all(exp.CTE))
    assert scopes[id(cte_body)] == frozenset({_key("id")})
    assert scopes[id(tree)] == frozenset({_key("id")})


def test_declared_model_key_unions_with_sql_derived_key() -> None:
    """A native PRIMARY KEY declared on the model and a DISTINCT-derived key both
    hold, so the model carries both (reconcile by meet, no conflict)."""
    src = _source("source.shop.raw.orders")
    keys = _keys(
        src,
        _model(
            "model.shop.d",
            "SELECT DISTINCT customer_id, region FROM orders",
            constraints=(
                ConstraintSpec(type=ConstraintType.PRIMARY_KEY, columns=("customer_id",)),
            ),
        ),
    )
    assert keys["model.shop.d"] == CandidateKeySet.of(
        _key("customer_id"), _key("customer_id", "region")
    )
