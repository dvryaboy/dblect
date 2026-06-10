# pyright: reportInvalidTypeForm=false, reportUnusedClass=false
"""Foreign-key edges from both sources, merged.

A project states a foreign key two ways, and dblect reads both into one edge
representation: a ``ForeignKey(...)`` marker on a contract, and an existing dbt
``relationships`` test (read "for free", the same way a ``unique`` test is read
as a key). The two are merged and de-duplicated, so declaring a relationship dbt
already tests does not double it. See ``docs/design/declaration-dsl.md``.

These pin the edge *production and merge*; what consumes the edge (a fan-out
finding, contract-directed fixture generation) is a later build, so there is no
propagation here to assert against.
"""

from pathlib import Path

from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import DbtTestMetadata
from dblect.types import (
    Currency,
    ForeignKey,
    ForeignKeyEdge,
    ModelContract,
    Money,
    PrimaryKey,
    dbt_relationship_edges,
    foreign_key_edges,
)

_ORDERS = SourceRef(SourceKind.MODEL, "model.shop.orders")
_CUSTOMERS = SourceRef(SourceKind.MODEL, "model.shop.customers")


def _model(uid: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=("shop", uid.split(".")[-1]),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _relationships_test(
    uid: str,
    *,
    child: str,
    child_column: str,
    parent: str,
    parent_column: str | None,
    to: str | None = None,
    enabled: bool = True,
) -> Node:
    kwargs: dict[str, str] = {
        "column_name": child_column,
        "to": to or f"ref('{parent.split('.')[-1]}')",
    }
    if parent_column is not None:
        kwargs["field"] = parent_column
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.OTHER,
        fqn=("shop", uid.split(".")[-1]),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({child, parent}),
        attached_node=child,
        test_metadata=DbtTestMetadata(name="relationships", kwargs=kwargs, enabled=enabled),
    )


def _manifest(*nodes: Node) -> Manifest:
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


_EXPECTED = ForeignKeyEdge(
    child=ColumnRef(_ORDERS, "customer_id"),
    parent=ColumnRef(_CUSTOMERS, "customer_id"),
)


def test_relationships_test_becomes_an_edge() -> None:
    manifest = _manifest(
        _model("model.shop.orders"),
        _model("model.shop.customers"),
        _relationships_test(
            "test.shop.rel",
            child="model.shop.orders",
            child_column="customer_id",
            parent="model.shop.customers",
            parent_column="customer_id",
        ),
    )
    assert dbt_relationship_edges(manifest) == (_EXPECTED,)


def test_disabled_relationships_test_is_ignored() -> None:
    manifest = _manifest(
        _model("model.shop.orders"),
        _model("model.shop.customers"),
        _relationships_test(
            "test.shop.rel",
            child="model.shop.orders",
            child_column="customer_id",
            parent="model.shop.customers",
            parent_column="customer_id",
            enabled=False,
        ),
    )
    assert dbt_relationship_edges(manifest) == ()


def test_relationships_test_missing_parent_column_is_skipped() -> None:
    broken = _relationships_test(
        "test.shop.rel",
        child="model.shop.orders",
        child_column="customer_id",
        parent="model.shop.customers",
        parent_column=None,  # no `field`: the parent column is unknown
    )
    manifest = _manifest(_model("model.shop.orders"), _model("model.shop.customers"), broken)
    assert dbt_relationship_edges(manifest) == ()


def test_foreign_key_edges_merges_contract_and_dbt_sources(
    registry: object,
) -> None:
    manifest = _manifest(
        _model("model.shop.orders"),
        _model("model.shop.customers"),
        _model("model.shop.regions"),
        _relationships_test(
            "test.shop.rel",
            child="model.shop.orders",
            child_column="customer_id",
            parent="model.shop.customers",
            parent_column="customer_id",
        ),
    )

    class Orders(ModelContract):
        dbt_model = "orders"
        # the same edge the dbt test already states, declared again
        customer_id: ForeignKey("customers.customer_id")
        # plus one only the contract knows
        region_id: ForeignKey("regions.region_id")
        amount: Money(currency=Currency.USD)

    edges = foreign_key_edges(manifest)
    region_edge = ForeignKeyEdge(
        child=ColumnRef(_ORDERS, "region_id"),
        parent=ColumnRef(SourceRef(SourceKind.MODEL, "model.shop.regions"), "region_id"),
    )
    assert set(edges) == {_EXPECTED, region_edge}
    assert len(edges) == 2  # the doubly-declared edge appears once


def test_relationships_against_real_jaffle(jaffle_manifest_path: Path) -> None:
    manifest = Manifest.from_file(jaffle_manifest_path)
    orders = SourceRef(SourceKind.MODEL, "model.jaffle_shop.orders")
    customers = SourceRef(SourceKind.MODEL, "model.jaffle_shop.customers")
    expected = ForeignKeyEdge(
        child=ColumnRef(orders, "customer_id"),
        parent=ColumnRef(customers, "customer_id"),
    )
    assert expected in set(dbt_relationship_edges(manifest))


def test_primary_key_and_foreign_key_stay_separate_concerns() -> None:
    """A model carrying both a PrimaryKey and a ForeignKey contributes a key fact
    and an edge respectively; neither absorbs the other."""
    manifest = _manifest(_model("model.shop.orders"), _model("model.shop.customers"))

    class Orders(ModelContract):
        dbt_model = "orders"
        order_id: PrimaryKey
        customer_id: ForeignKey("customers.customer_id")

    edges = foreign_key_edges(manifest)
    assert edges == (
        ForeignKeyEdge(
            child=ColumnRef(_ORDERS, "customer_id"),
            parent=ColumnRef(_CUSTOMERS, "customer_id"),
        ),
    )
