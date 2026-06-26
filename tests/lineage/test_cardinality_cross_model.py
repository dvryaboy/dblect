"""End-to-end cross-model validation for the fan-out (cardinality inflation) hazard.

The cross-model cardinality follow-on in ``docs/design/hazard-algebra.md`` rests on a
hypothesis worth settling before any detector is written: that no new propagated property
is needed. An un-collapsed fan-out already degrades the producing model's propagated
``uniqueness`` to ``NO_KEYS`` (the join drops the key the many-side breaks), and
``where_provenance`` already traces a downstream aggregate's column back to the source on
the replicated side. The cross-model fan-trap finding then reads ``grain_preserved`` over
the propagated uniqueness, with ``where_provenance`` supplying the origin column, rather
than carrying a new replicated-side value.

These pin that the two existing properties carry the signal end to end over a real
multi-model manifest, and that they compose to the right fire/silent decision. They are the
acceptance tests the follow-on detector is built against: if they hold, the work is a
finding that joins two properties, not a new lattice/grounding/propagation triple.

The shop has orders (keyed on ``order_id``) and order_items (many rows per order). A staging
model that joins them without collapsing replicates each order's ``amount`` across its
items; a downstream ``SUM(amount)`` is the fan trap. A staging model that groups back to the
order grain, or a join whose many-side is itself unique on the key, is benign.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_manifest_graph, build_relation_graph
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties import where_provenance
from dblect.lineage.properties.uniqueness import (
    NO_KEYS,
    CandidateKeySet,
    Key,
    grain_preserved,
    uniqueness_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType

_DUCKDB = profile_for_adapter("duckdb")

_ORDERS = "source.shop.raw.orders"
_ITEMS = "source.shop.raw.order_items"
_STG = "model.shop.stg_order_items"
_MART = "model.shop.mart_revenue"


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


def _manifest(*nodes: Node) -> Manifest:
    return Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )


def _model_keys(manifest: Manifest) -> dict[str, CandidateKeySet]:
    result = build_relation_graph(manifest)
    anns = propagate(result.graph, uniqueness_property(manifest, _DUCKDB))
    return {ref.unique_id: ann.value for ref, ann in anns.items() if ref.kind is SourceKind.MODEL}


def _provenance(manifest: Manifest) -> Mapping[ColumnRef, frozenset[ColumnRef]]:
    result = build_manifest_graph(manifest)
    anns = propagate(result.graph, where_provenance)
    return {ref: ann.value for ref, ann in anns.items()}


def _key(*cols: str) -> Key:
    return frozenset(cols)


# The uncovered join: order_items carries no key, so a row per item survives.
_UNCOLLAPSED_STG = _model(
    _STG,
    "SELECT o.order_id, o.amount, i.item_id "
    "FROM orders o JOIN order_items i ON o.order_id = i.order_id",
)
# The same join, collapsed back to the order grain.
_COLLAPSED_STG = _model(
    _STG,
    "SELECT o.order_id, SUM(o.amount) AS amount "
    "FROM orders o JOIN order_items i ON o.order_id = i.order_id GROUP BY o.order_id",
)


def _orders_with_key() -> tuple[Node, Node]:
    src = _source(_ORDERS)
    return src, _unique("test.shop.orders_pk", column="order_id", target=_ORDERS)


# --- the gate: an un-collapsed fan-out degrades the producing model's uniqueness ----------


def test_uncollapsed_fanout_degrades_staging_uniqueness_to_no_keys() -> None:
    """The join to the keyless many-side drops the order key: the staging output is provably
    keyed on nothing, the signal a downstream sum's fan trap rests on."""
    orders, orders_pk = _orders_with_key()
    keys = _model_keys(_manifest(orders, orders_pk, _source(_ITEMS), _UNCOLLAPSED_STG))
    assert keys[_STG] == NO_KEYS


def test_covered_join_preserves_staging_uniqueness() -> None:
    """When the many-side is itself unique on the join key the join is one-to-one, so the
    order key survives and no fan-out is signalled."""
    orders, orders_pk = _orders_with_key()
    items_pk = _unique("test.shop.items_pk", column="order_id", target=_ITEMS)
    keys = _model_keys(_manifest(orders, orders_pk, _source(_ITEMS), items_pk, _UNCOLLAPSED_STG))
    assert keys[_STG] == CandidateKeySet.of(_key("order_id"))


def test_groupby_collapse_restores_staging_uniqueness() -> None:
    """Grouping back to the order grain re-introduces the order key, so the fan-out is
    collapsed before export and nothing downstream should fire."""
    orders, orders_pk = _orders_with_key()
    keys = _model_keys(_manifest(orders, orders_pk, _source(_ITEMS), _COLLAPSED_STG))
    assert keys[_STG] == CandidateKeySet.of(_key("order_id"))


# --- the trace: where-provenance reaches the replicated source cross-model ----------------


def test_downstream_sum_traces_to_the_replicated_source_column() -> None:
    """``SUM(amount)`` in the mart traces, through the staging join and the mart's own
    GROUP BY, back to ``orders.amount`` (the replicated side), the origin column the
    discriminator needs to find the grain the magnitude was produced at."""
    orders, orders_pk = _orders_with_key()
    mart = _model(
        _MART, "SELECT order_id, SUM(amount) AS total FROM stg_order_items GROUP BY order_id"
    )
    prov = _provenance(_manifest(orders, orders_pk, _source(_ITEMS), _UNCOLLAPSED_STG, mart))
    total = ColumnRef(SourceRef(SourceKind.MODEL, _MART), "total")
    assert prov[total] == frozenset({ColumnRef(SourceRef(SourceKind.SOURCE, _ORDERS), "amount")})


def test_provenance_distinguishes_joined_in_from_replicated() -> None:
    """The joined-in side is reachable too: a passthrough of ``item_id`` traces to
    ``order_items``, so the discriminator can tell a replicated-side read (the fan trap)
    from a joined-in read (the intended set aggregation)."""
    orders, orders_pk = _orders_with_key()
    mart = _model(_MART, "SELECT order_id, item_id FROM stg_order_items")
    prov = _provenance(_manifest(orders, orders_pk, _source(_ITEMS), _UNCOLLAPSED_STG, mart))
    item = ColumnRef(SourceRef(SourceKind.MODEL, _MART), "item_id")
    assert prov[item] == frozenset({ColumnRef(SourceRef(SourceKind.SOURCE, _ITEMS), "item_id")})


# --- the synthesis: the two properties compose to the right decision via grain_preserved --


def test_signals_compose_to_fire_on_the_fan_trap() -> None:
    """The end-to-end hypothesis: the propagated staging uniqueness, fed to ``grain_preserved``
    with the origin key recovered from the replicated source, decides the fan trap with no new
    property. Un-collapsed staging keyed on nothing, origin keyed on ``order_id`` -> not
    preserved -> fire."""
    orders, orders_pk = _orders_with_key()
    mart = _model(
        _MART, "SELECT order_id, SUM(amount) AS total FROM stg_order_items GROUP BY order_id"
    )
    manifest = _manifest(orders, orders_pk, _source(_ITEMS), _UNCOLLAPSED_STG, mart)

    staging_keys = _model_keys(manifest)[_STG]
    origin_key = _key("order_id")  # orders' propagated key, where SUM(amount) is single-counted
    assert not grain_preserved(staging_keys, origin_key)


def test_signals_compose_to_stay_silent_after_collapse() -> None:
    """The benign counterpart: collapsed staging is keyed on ``order_id``, which refines the
    origin key, so the same composition stays silent."""
    orders, orders_pk = _orders_with_key()
    mart = _model(
        _MART, "SELECT order_id, SUM(amount) AS total FROM stg_order_items GROUP BY order_id"
    )
    manifest = _manifest(orders, orders_pk, _source(_ITEMS), _COLLAPSED_STG, mart)

    staging_keys = _model_keys(manifest)[_STG]
    origin_key = _key("order_id")
    assert grain_preserved(staging_keys, origin_key)
