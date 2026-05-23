"""Tests for declaration-derived uniqueness facts and manifest aggregation.

SQL-level propagation (the structural-proof and CTE-pass-through cases the
old `facts_from_sql` covered) lives in `test_propagation.py`; this module
focuses on the declaration ingestion layer and the cross-cutting
`facts_from_manifest` aggregation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dblect.manifest import (
    Column,
    ConstraintSpec,
    ConstraintType,
    DbtTestMetadata,
    Manifest,
    Node,
    ResourceType,
)
from dblect.uniqueness import (
    UniquenessSource,
    facts_from_declarations,
    facts_from_manifest,
)


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
    manifest = Manifest(
        schema_version="x", adapter_type="duckdb", nodes={model.unique_id: model}
    )
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
        adapter_type="duckdb",
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
        constraints=(ConstraintSpec(type=ConstraintType.PRIMARY_KEY, columns=("id",)),),
    )
    manifest = Manifest(
        schema_version="x", adapter_type="duckdb", nodes={model.unique_id: model}
    )
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
            constraints=(ConstraintSpec(type=ConstraintType.UNIQUE),),
        )},
    )
    manifest = Manifest(
        schema_version="x", adapter_type="duckdb", nodes={model.unique_id: model}
    )
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
            constraints=(ConstraintSpec(type=ConstraintType.NOT_NULL),),
        )},
    )
    manifest = Manifest(
        schema_version="x", adapter_type="duckdb", nodes={model.unique_id: model}
    )
    facts = facts_from_manifest(manifest)
    assert facts == {}


def test_unique_test_with_where_filter_does_not_ground_fact() -> None:
    # A `where`-filtered test only proves uniqueness within the filtered
    # subset; downstream detectors assume facts are unconditional, so we
    # skip rather than over-claim.
    model = _node(
        unique_id="model.pkg.orders",
        name="orders",
        resource_type=ResourceType.MODEL,
    )
    test_node = _node(
        unique_id="test.pkg.unique_us_orders",
        name="unique_us_orders_id",
        resource_type=ResourceType.OTHER,
        test_metadata=DbtTestMetadata(
            name="unique",
            kwargs={"column_name": "order_id"},
            where="country = 'US'",
        ),
        attached_node="model.pkg.orders",
    )
    manifest = Manifest(
        schema_version="x",
        adapter_type="duckdb",
        nodes={model.unique_id: model, test_node.unique_id: test_node},
    )
    assert facts_from_declarations(manifest) == ()


def test_disabled_unique_test_does_not_ground_fact() -> None:
    model = _node(
        unique_id="model.pkg.orders",
        name="orders",
        resource_type=ResourceType.MODEL,
    )
    test_node = _node(
        unique_id="test.pkg.unique_orders_disabled",
        name="unique_orders_id",
        resource_type=ResourceType.OTHER,
        test_metadata=DbtTestMetadata(
            name="unique",
            kwargs={"column_name": "order_id"},
            enabled=False,
        ),
        attached_node="model.pkg.orders",
    )
    manifest = Manifest(
        schema_version="x",
        adapter_type="duckdb",
        nodes={model.unique_id: model, test_node.unique_id: test_node},
    )
    assert facts_from_declarations(manifest) == ()


def test_unique_combination_test_with_where_is_also_skipped() -> None:
    # Same conditional-uniqueness concern applies to composite-key tests.
    model = _node(
        unique_id="model.pkg.orders",
        name="orders",
        resource_type=ResourceType.MODEL,
    )
    test_node = _node(
        unique_id="test.pkg.combo_us",
        name="unique_combo_us",
        resource_type=ResourceType.OTHER,
        test_metadata=DbtTestMetadata(
            name="dbt_utils.unique_combination_of_columns",
            kwargs={"combination_of_columns": ["customer_id", "order_date"]},
            where="country = 'US'",
        ),
        attached_node="model.pkg.orders",
    )
    manifest = Manifest(
        schema_version="x",
        adapter_type="duckdb",
        nodes={model.unique_id: model, test_node.unique_id: test_node},
    )
    assert facts_from_declarations(manifest) == ()


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
    manifest = Manifest(
        schema_version="x",
        adapter_type="duckdb",
        nodes={test_node.unique_id: test_node},
    )
    assert facts_from_manifest(manifest) == {}


def test_declaration_fact_carries_provenance_detail(jaffle: Manifest) -> None:
    # The declared unique test on `customers.customer_id` carries a detail
    # naming the test node so reviewers can navigate to it. Propagation may
    # also surface a fact on the same column set; this assertion targets the
    # declaration-sourced one specifically.
    facts = facts_from_manifest(jaffle)
    customers = facts["model.jaffle_shop.customers"]
    declared = [
        f
        for f in customers
        if f.columns == frozenset({"customer_id"})
        and f.source is UniquenessSource.DBT_UNIQUE_TEST
    ]
    assert len(declared) == 1
    assert declared[0].detail is not None
    assert "customer_id" in declared[0].detail


# --- facts_from_manifest combines declarations + propagation ---


def test_facts_from_declarations_excludes_propagation() -> None:
    model = _node(
        unique_id="model.pkg.x",
        name="x",
        resource_type=ResourceType.MODEL,
        compiled_code="select distinct a from t",
    )
    manifest = Manifest(
        schema_version="x", adapter_type="duckdb", nodes={model.unique_id: model}
    )
    # Declarations alone: no facts (no tests, no constraints).
    assert facts_from_declarations(manifest) == ()
    # Combined: the propagation-derived DISTINCT fact appears.
    combined = facts_from_manifest(manifest)
    assert "model.pkg.x" in combined
    assert any(
        f.source is UniquenessSource.STRUCTURAL_PROOF for f in combined["model.pkg.x"]
    )


def _node(
    *,
    unique_id: str,
    name: str,
    resource_type: ResourceType,
    columns: dict[str, Column] | None = None,
    constraints: tuple[ConstraintSpec, ...] = (),
    test_metadata: DbtTestMetadata | None = None,
    attached_node: str | None = None,
    raw_code: str | None = None,
    compiled_code: str | None = None,
) -> Node:
    # The structural-proof layer reads compiled_code by default; tests that
    # care about SQL-derived facts should set compiled_code. raw_code is
    # accepted for callers that want to model the on-disk template (and is
    # what suppression directives read from in the walker).
    return Node(
        unique_id=unique_id,
        name=name,
        resource_type=resource_type,
        fqn=("pkg", name),
        package_name="pkg",
        schema=None,
        raw_code=raw_code,
        compiled_code=compiled_code,
        original_file_path=None,
        columns=columns or {},
        depends_on=frozenset(),
        constraints=constraints,
        test_metadata=test_metadata,
        attached_node=attached_node,
    )
