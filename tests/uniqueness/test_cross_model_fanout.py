"""Cross-model fan-out: a downstream additive aggregate over a magnitude an upstream
fan-out replicated, with the grain never collapsed back.

The local ``detect_join_fanout`` fires at the join that can multiply rows. It cannot see a
*downstream* model that then sums a replicated magnitude: that mart has no join of its own,
so nothing flags the double count today. ``detect_cross_model_fanout`` closes that gap by
reading two propagated properties at the consumer: the aggregated relation's ``uniqueness``
(is it still keyed at the magnitude's grain) and ``where_provenance`` (which source the
magnitude traces to, so the grain it is single-counted at can be recovered). The decision is
``grain_preserved`` over the propagated keys, with the origin key translated into the
aggregated relation's column names through provenance.

These pin the contract at the boundary, including the discriminator's two edges: an additive
aggregate of a *replicated-side* magnitude over a relation no longer keyed at that grain
fires; the same aggregate of a *joined-in-side* magnitude, whose grain the relation does
preserve, stays silent. The firewall edge (no known origin grain to claim) stays silent too.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType
from dblect.sql import Finding, FindingKind, parse_sql
from dblect.uniqueness.detector import make_cross_model_fanout_detectors

_DUCKDB = profile_for_adapter("duckdb")

_ORDERS = "source.shop.raw.orders"
_ITEMS = "source.shop.raw.order_items"
_STG = "model.shop.stg_order_items"
_MART = "model.shop.mart"

# Staging joins orders (one row per order) to order_items (many per order) and projects a
# column from each side: ``amount`` is replicated across an order's items, ``qty`` sits at
# the line grain. How order_items is keyed decides staging's grain, so the fixtures vary it.
_STG_SQL = (
    "SELECT o.order_id, o.amount, i.item_id, i.qty "
    "FROM orders o JOIN order_items i ON o.order_id = i.order_id"
)
# Staging that collapses back to the order grain before exporting.
_STG_COLLAPSED_SQL = (
    "SELECT o.order_id, SUM(o.amount) AS amount "
    "FROM orders o JOIN order_items i ON o.order_id = i.order_id GROUP BY o.order_id"
)


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


def _findings(manifest: Manifest, consumer_uid: str) -> tuple[Finding, ...]:
    """Run the cross-model fan-out detectors over one consumer model's compiled SQL."""
    detectors = make_cross_model_fanout_detectors(manifest, _DUCKDB)
    node = manifest.nodes[consumer_uid]
    assert node.compiled_code is not None
    tree = parse_sql(node.compiled_code, dialect="duckdb")
    return tuple(f for detect in detectors for f in detect(tree))


def _shop(*extra: Node, items_unique_on: str | None, mart_sql: str, stg_sql: str = _STG_SQL):
    """The orders/order_items/staging/mart manifest, varying the order_items key and the
    mart's aggregate. Orders is keyed on ``order_id`` unless overridden via ``extra``."""
    nodes: list[Node] = [
        _source(_ORDERS),
        _unique("test.shop.orders_pk", column="order_id", target=_ORDERS),
        _source(_ITEMS),
        _model(_STG, stg_sql),
        _model(_MART, mart_sql),
        *extra,
    ]
    if items_unique_on is not None:
        nodes.append(_unique("test.shop.items_pk", column=items_unique_on, target=_ITEMS))
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


_SUM_AMOUNT = "SELECT order_id, SUM(amount) AS total FROM stg_order_items GROUP BY order_id"
_SUM_QTY = "SELECT order_id, SUM(qty) AS total_qty FROM stg_order_items GROUP BY order_id"
_MAX_AMOUNT = "SELECT order_id, MAX(amount) AS top FROM stg_order_items GROUP BY order_id"


# --- fires: an additive aggregate of a replicated magnitude over a broken grain ----------


def test_sum_of_replicated_magnitude_over_keyless_staging_fires() -> None:
    """order_items carries no key, so staging is keyed on nothing; ``SUM(amount)`` folds the
    replicated order amount and double counts."""
    manifest = _shop(items_unique_on=None, mart_sql=_SUM_AMOUNT)
    findings = _findings(manifest, _MART)
    assert [f.kind for f in findings] == [FindingKind.CROSS_MODEL_FANOUT]


def test_sum_of_replicated_magnitude_over_line_grain_staging_fires() -> None:
    """The sharper case: order_items is keyed on ``item_id``, so staging is perfectly unique,
    but at the *line* grain. ``SUM(amount)`` still double counts the order amount, since the
    surviving key does not refine the order grain the amount is single-counted at."""
    manifest = _shop(items_unique_on="item_id", mart_sql=_SUM_AMOUNT)
    findings = _findings(manifest, _MART)
    assert [f.kind for f in findings] == [FindingKind.CROSS_MODEL_FANOUT]


# --- stays silent: the joined-in magnitude, the collapse, the covered join, the safe fold -


def test_sum_of_joined_in_magnitude_at_its_own_grain_is_silent() -> None:
    """``qty`` traces to order_items, whose ``item_id`` key staging preserves, so summing it
    per order is the intended set aggregation, not a fan trap."""
    manifest = _shop(items_unique_on="item_id", mart_sql=_SUM_QTY)
    assert _findings(manifest, _MART) == ()


def test_groupby_collapse_in_staging_is_silent() -> None:
    """Staging groups back to the order grain, so its export is keyed on ``order_id`` and the
    downstream ``SUM(amount)`` reads one row per order."""
    manifest = _shop(items_unique_on=None, mart_sql=_SUM_AMOUNT, stg_sql=_STG_COLLAPSED_SQL)
    assert _findings(manifest, _MART) == ()


def test_covered_join_is_silent() -> None:
    """order_items is itself unique on the join key, so the join is one-to-one and staging
    stays keyed on ``order_id``."""
    manifest = _shop(items_unique_on="order_id", mart_sql=_SUM_AMOUNT)
    assert _findings(manifest, _MART) == ()


def test_duplicate_safe_aggregate_is_silent() -> None:
    """``MAX`` is idempotent under duplication, so replicating rows cannot change it."""
    manifest = _shop(items_unique_on=None, mart_sql=_MAX_AMOUNT)
    assert _findings(manifest, _MART) == ()


def test_no_known_origin_grain_is_silent() -> None:
    """The firewall: with no key on orders we cannot name the grain ``amount`` is
    single-counted at, so there is no positive fact to fire on."""
    nodes = [
        _source(_ORDERS),  # no unique test on orders
        _source(_ITEMS),
        _unique("test.shop.items_pk", column="item_id", target=_ITEMS),
        _model(_STG, _STG_SQL),
        _model(_MART, _SUM_AMOUNT),
    ]
    manifest = Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )
    assert _findings(manifest, _MART) == ()
