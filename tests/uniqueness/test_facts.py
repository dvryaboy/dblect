"""Tests for declaration-derived uniqueness facts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dblect.manifest import (
    Column,
    ConstraintSpec,
    DbtTestMetadata,
    Manifest,
    Node,
    ResourceType,
)
from dblect.uniqueness import UniquenessFact, UniquenessSource, facts_from_manifest


@pytest.fixture(scope="module")
def jaffle(jaffle_manifest_path: Path) -> Manifest:
    return Manifest.from_file(jaffle_manifest_path)


def test_picks_up_unique_tests_from_jaffle(jaffle: Manifest) -> None:
    facts = facts_from_manifest(jaffle)
    # jaffle declares unique tests on customers.customer_id, orders.order_id,
    # and stg_customers.customer_id (at minimum).
    customers = facts.get("model.jaffle_shop.customers", ())
    assert any(
        f.columns == frozenset({"customer_id"})
        and f.source is UniquenessSource.DBT_UNIQUE_TEST
        for f in customers
    )
    orders = facts.get("model.jaffle_shop.orders", ())
    assert any(
        f.columns == frozenset({"order_id"})
        and f.source is UniquenessSource.DBT_UNIQUE_TEST
        for f in orders
    )


def test_models_without_facts_are_absent_from_mapping() -> None:
    # A bare model with no tests and no constraints produces no facts.
    model = _node(
        unique_id="model.pkg.alone",
        name="alone",
        resource_type=ResourceType.MODEL,
    )
    manifest = Manifest(schema_version="x", nodes={model.unique_id: model})
    facts = facts_from_manifest(manifest)
    assert facts == {}


def test_unique_combination_of_columns_test_produces_composite_fact() -> None:
    # Synthesize a test node that matches dbt-utils' shape.
    test_node = _node(
        unique_id="test.pkg.combo",
        name="unique_combination_orders",
        resource_type=ResourceType.OTHER,
        test_metadata=DbtTestMetadata(
            name="dbt_utils.unique_combination_of_columns",
            kwargs={"combination_of_columns": ["customer_id", "order_date"]},
        ),
        attached_node="model.pkg.orders",
    )
    model = _node(
        unique_id="model.pkg.orders",
        name="orders",
        resource_type=ResourceType.MODEL,
    )
    manifest = Manifest(
        schema_version="x",
        nodes={model.unique_id: model, test_node.unique_id: test_node},
    )
    facts = facts_from_manifest(manifest)
    [combo] = facts["model.pkg.orders"]
    assert combo.columns == frozenset({"customer_id", "order_date"})
    assert combo.source is UniquenessSource.DBT_UNIQUE_COMBINATION_TEST


def test_native_model_level_primary_key_constraint() -> None:
    model = _node(
        unique_id="model.pkg.x",
        name="x",
        resource_type=ResourceType.MODEL,
        constraints=(ConstraintSpec(type="primary_key", columns=("id",)),),
    )
    manifest = Manifest(schema_version="x", nodes={model.unique_id: model})
    facts = facts_from_manifest(manifest)
    [fact] = facts["model.pkg.x"]
    assert fact.columns == frozenset({"id"})
    assert fact.source is UniquenessSource.NATIVE_CONSTRAINT


def test_native_column_level_unique_constraint() -> None:
    model = _node(
        unique_id="model.pkg.x",
        name="x",
        resource_type=ResourceType.MODEL,
        columns={"slug": Column(
            name="slug",
            data_type=None,
            description=None,
            constraints=(ConstraintSpec(type="unique"),),
        )},
    )
    manifest = Manifest(schema_version="x", nodes={model.unique_id: model})
    facts = facts_from_manifest(manifest)
    [fact] = facts["model.pkg.x"]
    assert fact.columns == frozenset({"slug"})
    assert fact.source is UniquenessSource.NATIVE_CONSTRAINT


def test_not_null_constraint_is_not_a_uniqueness_fact() -> None:
    model = _node(
        unique_id="model.pkg.x",
        name="x",
        resource_type=ResourceType.MODEL,
        columns={"slug": Column(
            name="slug",
            data_type=None,
            description=None,
            constraints=(ConstraintSpec(type="not_null"),),
        )},
    )
    manifest = Manifest(schema_version="x", nodes={model.unique_id: model})
    facts = facts_from_manifest(manifest)
    assert facts == {}


def test_unique_test_on_source_is_skipped() -> None:
    # Sources can carry tests, but uniqueness reasoning on sources is a
    # different problem; we restrict to models.
    test_node = _node(
        unique_id="test.pkg.unique_raw",
        name="unique_raw_id",
        resource_type=ResourceType.OTHER,
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": "id"}),
        attached_node="source.pkg.raw.raw",
    )
    manifest = Manifest(schema_version="x", nodes={test_node.unique_id: test_node})
    assert facts_from_manifest(manifest) == {}


def test_fact_carries_provenance_detail(jaffle: Manifest) -> None:
    facts = facts_from_manifest(jaffle)
    customers = facts["model.jaffle_shop.customers"]
    [fact] = [f for f in customers if f.columns == frozenset({"customer_id"})]
    # detail should reference the test node's name so reviewers can navigate.
    assert fact.detail is not None
    assert "customer_id" in fact.detail


def test_uniqueness_fact_is_hashable_and_freezable() -> None:
    f = UniquenessFact(
        model_unique_id="model.pkg.x",
        columns=frozenset({"a", "b"}),
        source=UniquenessSource.DBT_UNIQUE_TEST,
        detail="t",
    )
    # Hashable, so the facts can live in sets and dicts.
    assert hash(f) == hash(replace(f))


def _node(
    *,
    unique_id: str,
    name: str,
    resource_type: ResourceType,
    columns: dict[str, Column] | None = None,
    constraints: tuple[ConstraintSpec, ...] = (),
    test_metadata: DbtTestMetadata | None = None,
    attached_node: str | None = None,
) -> Node:
    return Node(
        unique_id=unique_id,
        name=name,
        resource_type=resource_type,
        fqn=("pkg", name),
        package_name="pkg",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns=columns or {},
        depends_on=frozenset(),
        constraints=constraints,
        test_metadata=test_metadata,
        attached_node=attached_node,
    )
