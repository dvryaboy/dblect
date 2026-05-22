"""Manifest parsing tests against the vendored jaffle_shop_duckdb fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from dblect.manifest import Manifest, ResourceType


@pytest.fixture(scope="module")
def jaffle(jaffle_manifest_path: Path) -> Manifest:
    return Manifest.from_file(jaffle_manifest_path)


def test_loads_schema_version(jaffle: Manifest) -> None:
    assert jaffle.schema_version.startswith("https://schemas.getdbt.com/dbt/manifest/")


def test_loads_adapter_type(jaffle: Manifest) -> None:
    # The jaffle fixture is generated with the duckdb adapter.
    assert jaffle.adapter_type == "duckdb"


def test_partitions_nodes_by_resource_type(jaffle: Manifest) -> None:
    assert len(jaffle.models) == 5
    assert len(jaffle.seeds) == 3
    assert len(jaffle.sources) == 0


def test_models_have_expected_names(jaffle: Manifest) -> None:
    names = {n.name for n in jaffle.models.values()}
    assert names == {"customers", "orders", "stg_customers", "stg_orders", "stg_payments"}


def test_models_carry_raw_and_compiled_code(jaffle: Manifest) -> None:
    # `dbt compile` populates both raw_code (the on-disk template) and
    # compiled_code (the rendered SQL the analysis layer consumes).
    customers = jaffle.nodes["model.jaffle_shop.customers"]
    assert customers.raw_code is not None
    assert "stg_customers" in customers.raw_code
    assert customers.compiled_code is not None
    # Rendered ref('stg_customers') resolves to the relation name.
    assert "stg_customers" in customers.compiled_code


def test_models_have_column_metadata(jaffle: Manifest) -> None:
    orders = jaffle.nodes["model.jaffle_shop.orders"]
    assert set(orders.columns.keys()) == {
        "order_id",
        "customer_id",
        "order_date",
        "status",
        "amount",
        "credit_card_amount",
        "coupon_amount",
        "bank_transfer_amount",
        "gift_card_amount",
    }


def test_node_resource_types_are_typed(jaffle: Manifest) -> None:
    for n in jaffle.nodes.values():
        # Should be a ResourceType, not a bare string.
        assert isinstance(n.resource_type, ResourceType)


def test_depends_on_reflects_refs(jaffle: Manifest) -> None:
    customers = jaffle.nodes["model.jaffle_shop.customers"]
    assert customers.depends_on == frozenset(
        {
            "model.jaffle_shop.stg_customers",
            "model.jaffle_shop.stg_orders",
            "model.jaffle_shop.stg_payments",
        }
    )


def test_dag_is_acyclic_and_in_topo_order(jaffle: Manifest) -> None:
    order = jaffle.dag.topological_order()
    assert len(order) == len(jaffle.nodes)
    position = {n: i for i, n in enumerate(order)}
    # Every dependency must appear before the node that depends on it.
    for n in jaffle.nodes.values():
        for upstream in n.depends_on:
            if upstream in position:
                assert position[upstream] < position[n.unique_id], (
                    f"{upstream} should precede {n.unique_id} in topological order"
                )


def test_dag_upstream_matches_depends_on(jaffle: Manifest) -> None:
    for n in jaffle.nodes.values():
        present = {u for u in n.depends_on if u in jaffle.nodes}
        assert jaffle.dag.upstream(n.unique_id) == present


def test_transitive_downstream_of_stg_orders(jaffle: Manifest) -> None:
    descendants = jaffle.dag.transitive_downstream("model.jaffle_shop.stg_orders")
    # stg_orders feeds both customers and orders directly.
    assert "model.jaffle_shop.customers" in descendants
    assert "model.jaffle_shop.orders" in descendants


def test_seeds_have_no_dependencies(jaffle: Manifest) -> None:
    for n in jaffle.seeds.values():
        # Seeds are root nodes in the data-flow DAG.
        assert n.depends_on == frozenset()


def test_jaffle_tests_round_trip_with_default_test_config(jaffle: Manifest) -> None:
    # jaffle's generic tests are all built-in, enabled, and unfiltered: the
    # parser should surface those as the defaults on DbtTestMetadata.
    tests = [n for n in jaffle.nodes.values() if n.test_metadata is not None]
    assert tests, "jaffle fixture should expose at least one test node"
    for n in tests:
        tm = n.test_metadata
        assert tm is not None  # for the type checker
        assert tm.enabled is True
        assert tm.where is None
        # All of jaffle's tests are built-in (no third-party namespace).
        assert tm.namespace is None
