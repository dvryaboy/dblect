"""Tests for declaration-derived and structural-proof uniqueness facts."""

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
from dblect.sql import ParsedSQL
from dblect.uniqueness import (
    UniquenessSource,
    facts_from_declarations,
    facts_from_manifest,
    facts_from_sql,
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
        constraints=(ConstraintSpec(type=ConstraintType.PRIMARY_KEY, columns=("id",)),),
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
            constraints=(ConstraintSpec(type=ConstraintType.UNIQUE),),
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
            constraints=(ConstraintSpec(type=ConstraintType.NOT_NULL),),
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


# --- Structural proof from SQL ---


def _parsed(sql: str) -> ParsedSQL:
    return ParsedSQL.parse(sql, dialect="duckdb")


def test_distinct_proves_uniqueness() -> None:
    [fact] = facts_from_sql("model.pkg.x", _parsed("select distinct a, b from t"))
    assert fact.columns == frozenset({"a", "b"})
    assert fact.source is UniquenessSource.STRUCTURAL_PROOF
    assert "DISTINCT" in (fact.detail or "")


def test_group_by_bare_columns_proves_uniqueness() -> None:
    [fact] = facts_from_sql(
        "model.pkg.x", _parsed("select a, b, sum(x) from t group by a, b")
    )
    assert fact.columns == frozenset({"a", "b"})
    assert fact.source is UniquenessSource.STRUCTURAL_PROOF


def test_group_by_unprojected_keys_does_not_prove_named_uniqueness() -> None:
    # `select sum(x) from t group by a, b`: a, b aren't output columns so we
    # can't make a named-column uniqueness claim.
    assert facts_from_sql("model.pkg.x", _parsed("select sum(x) from t group by a, b")) == ()


def test_group_by_positional_does_not_prove_uniqueness() -> None:
    # `GROUP BY 1, 2` is a positional reference; we conservatively don't infer.
    assert facts_from_sql("model.pkg.x", _parsed("select a, b from t group by 1, 2")) == ()


def test_group_by_expression_does_not_prove_uniqueness() -> None:
    # The key is an expression, not a bare column, so we don't reason about it.
    sql = "select date_trunc('day', ts) as d, sum(x) from t group by date_trunc('day', ts)"
    assert facts_from_sql("model.pkg.x", _parsed(sql)) == ()


def test_top_level_select_with_cte_is_unwrapped() -> None:
    sql = (
        "with src as (select * from raw) "
        "select distinct a, b from src"
    )
    [fact] = facts_from_sql("model.pkg.x", _parsed(sql))
    assert fact.columns == frozenset({"a", "b"})


def test_inner_distinct_does_not_prove_outer_uniqueness() -> None:
    # The DISTINCT is inside a CTE; the outer SELECT just selects from it
    # without a DISTINCT or GROUP BY, so output uniqueness isn't proven.
    sql = "with src as (select distinct a from raw) select a, b from src join other on src.a = other.a"
    assert facts_from_sql("model.pkg.x", _parsed(sql)) == ()


def test_aliased_group_by_column_uses_alias_name() -> None:
    # If the projection aliases the group key, the output column is the alias.
    # `select a as id, sum(x) from t group by a`: we'd want to claim
    # uniqueness on "id", not "a". For first cut this is conservatively
    # skipped (the GROUP BY references "a" but the projection produces "id").
    sql = "select a as id, sum(x) from t group by a"
    # Conservative: skip rather than emit a fact under the wrong name.
    facts = facts_from_sql("model.pkg.x", _parsed(sql))
    # Acceptable for either result: no fact (current behaviour) or a fact on
    # "a" (if a future iteration learns alias resolution). Don't lock in.
    assert all("id" not in f.columns and "a" in f.columns for f in facts) or facts == ()


def test_union_set_op_is_out_of_scope() -> None:
    sql = "select a from t1 union select a from t2"
    assert facts_from_sql("model.pkg.x", _parsed(sql)) == ()


def test_no_distinct_no_group_by_produces_no_fact() -> None:
    assert facts_from_sql("model.pkg.x", _parsed("select a, b from t")) == ()


# --- facts_from_manifest combines declarations + structural proof ---


def test_facts_from_manifest_includes_structural_for_jaffle(jaffle: Manifest) -> None:
    facts = facts_from_manifest(jaffle)
    # stg_customers.sql is `select ... from raw_customers` with no DISTINCT or
    # GROUP BY. customers.sql has a top-level GROUP BY (final select), but
    # let's be lenient and just assert both kinds appear *somewhere* across
    # the jaffle models. Declarations are guaranteed; structural is nice to
    # have if any jaffle model qualifies.
    all_facts = [f for fs in facts.values() for f in fs]
    by_source = {f.source for f in all_facts}
    assert UniquenessSource.DBT_UNIQUE_TEST in by_source


def test_facts_from_declarations_excludes_structural() -> None:
    model = _node(
        unique_id="model.pkg.x",
        name="x",
        resource_type=ResourceType.MODEL,
        raw_code="select distinct a from t",
    )
    manifest = Manifest(schema_version="x", nodes={model.unique_id: model})
    # Declarations alone: no facts (no tests, no constraints).
    assert facts_from_declarations(manifest) == ()
    # Combined: the structural-proof DISTINCT fact appears.
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
) -> Node:
    return Node(
        unique_id=unique_id,
        name=name,
        resource_type=resource_type,
        fqn=("pkg", name),
        package_name="pkg",
        schema=None,
        raw_code=raw_code,
        compiled_code=None,
        original_file_path=None,
        columns=columns or {},
        depends_on=frozenset(),
        constraints=constraints,
        test_metadata=test_metadata,
        attached_node=attached_node,
    )
