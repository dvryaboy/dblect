# pyright: reportInvalidTypeForm=false, reportUnusedClass=false
"""Foreign-key edges from both sources, merged.

A project states a foreign key two ways, and dblect reads both into one edge
representation: a ``ForeignKey(...)`` marker on a contract, and an existing dbt
``relationships`` test (read "for free", the same way a ``unique`` test is read
as a key). The two are merged and de-duplicated, so declaring a relationship dbt
already tests does not double it. See ``docs/design/declaration-dsl.md``.

``relationship_tested_edges`` is the separate guard set the referential orphan-drop
check reads: only an edge covered by an enabled, unconditional, error-severity test
belongs to it, computed straight from the tests rather than from the merged edge list.

These pin the edge *production and merge*; what consumes the edge (a fan-out
finding, contract-directed fixture generation) is a later build, so there is no
propagation here to assert against.
"""

from pathlib import Path

import pytest

from dblect.demo import Currency, Money
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.manifest import DbtTestSeverity, Manifest, Node
from dblect.types import (
    ForeignKey,
    ForeignKeyEdge,
    ModelContract,
    PrimaryKey,
    dbt_relationship_edges,
    foreign_key_edges,
    relationship_tested_edges,
)
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import relationships_test as _relationships_test

_ORDERS = SourceRef(SourceKind.MODEL, "model.shop.orders")
_CUSTOMERS = SourceRef(SourceKind.MODEL, "model.shop.customers")

_EXPECTED = ForeignKeyEdge(
    child=ColumnRef(_ORDERS, "customer_id"),
    parent=ColumnRef(_CUSTOMERS, "customer_id"),
)


def _rel_test(
    *,
    enabled: bool = True,
    where: str | None = None,
    severity: DbtTestSeverity = DbtTestSeverity.ERROR,
) -> Node:
    return _relationships_test(
        "test.shop.rel",
        child="model.shop.orders",
        child_column="customer_id",
        parent="model.shop.customers",
        parent_column="customer_id",
        enabled=enabled,
        where=where,
        severity=severity,
    )


def test_relationships_test_becomes_an_edge() -> None:
    manifest = _manifest(_node("model.shop.orders"), _node("model.shop.customers"), _rel_test())
    assert dbt_relationship_edges(manifest) == (_EXPECTED,)


def test_disabled_relationships_test_is_ignored() -> None:
    manifest = _manifest(
        _node("model.shop.orders"), _node("model.shop.customers"), _rel_test(enabled=False)
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
    manifest = _manifest(_node("model.shop.orders"), _node("model.shop.customers"), broken)
    assert dbt_relationship_edges(manifest) == ()


def test_foreign_key_edges_merges_contract_and_dbt_sources(registry: object) -> None:
    manifest = _manifest(
        _node("model.shop.orders"),
        _node("model.shop.customers"),
        _node("model.shop.regions"),
        _rel_test(),
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
    manifest = _manifest(_node("model.shop.orders"), _node("model.shop.customers"))

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


# --- relationship_tested_edges: the guard set --------------------------------------

_EXPECTED_PAIR = (_EXPECTED.child, _EXPECTED.parent)


def test_enabled_unconditional_error_test_covers_its_edge() -> None:
    manifest = _manifest(_node("model.shop.orders"), _node("model.shop.customers"), _rel_test())
    assert relationship_tested_edges(manifest) == frozenset({_EXPECTED_PAIR})


@pytest.mark.parametrize(
    "test",
    [
        _rel_test(where="status != 'draft'"),
        _rel_test(enabled=False),
        _rel_test(severity=DbtTestSeverity.WARN),
    ],
    ids=["where-scoped", "disabled", "warn-severity"],
)
def test_a_condition_that_defeats_coverage(test: Node) -> None:
    manifest = _manifest(_node("model.shop.orders"), _node("model.shop.customers"), test)
    assert relationship_tested_edges(manifest) == frozenset()


def test_relationship_tested_edges_ignores_a_contract_only_edge(registry: object) -> None:
    # A contract-only edge (no relationships test at all) is not in the guard set,
    # even though it is a real, merged edge foreign_key_edges reports.
    manifest = _manifest(_node("model.shop.orders"), _node("model.shop.customers"))

    class Orders(ModelContract):
        dbt_model = "orders"
        customer_id: ForeignKey("customers.customer_id")

    assert relationship_tested_edges(manifest) == frozenset()
    assert foreign_key_edges(manifest) != ()
