"""Reading ``catalog.json`` and merging its columns into the manifest.

The catalog carries the warehouse-introspected column universe of every node,
including the DAG leaves (seeds, sources) that have no SQL to derive columns
from. These pin the parse and the merge contract: documented columns stay
authoritative, the catalog fills the rest, and a node the catalog never mentions
passes through untouched. See issue #77.
"""

from __future__ import annotations

from typing import Any

from dblect.lineage.builder import build_manifest_graph
from dblect.lineage.graph import SourceKind
from dblect.manifest import Catalog, Column, Manifest, Node, ResourceType


def _catalog_entry(uid: str, schema: str, name: str, **cols: str) -> dict[str, Any]:
    return {
        "metadata": {
            "type": "BASE TABLE",
            "schema": schema,
            "name": name,
            "database": "db",
            "comment": None,
            "owner": None,
        },
        "columns": {
            c: {"type": t, "index": i + 1, "name": c, "comment": None}
            for i, (c, t) in enumerate(cols.items())
        },
        "stats": {},
        "unique_id": uid,
    }


def _raw_catalog(*, nodes: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/catalog/v1.json",
            "dbt_version": "1.8.0",
            "generated_at": "2024-01-01T00:00:00Z",
            "invocation_id": "x",
            "env": {},
        },
        "nodes": nodes,
        "sources": sources,
        "errors": None,
    }


def _node(uid: str, *, kind: ResourceType, columns: dict[str, Column]) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=kind,
        fqn=tuple(uid.split(".")[1:]),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns=columns,
    )


def test_catalog_parses_node_and_source_columns() -> None:
    catalog = Catalog.from_raw(
        _raw_catalog(
            nodes={
                "seed.shop.codes": _catalog_entry(
                    "seed.shop.codes", "analytics", "codes", code="VARCHAR"
                )
            },
            sources={
                "source.shop.raw.payments": _catalog_entry(
                    "source.shop.raw.payments",
                    "raw",
                    "payments",
                    amount="DECIMAL",
                    currency="VARCHAR",
                )
            },
        )
    )
    assert catalog.columns_by_uid["seed.shop.codes"] == {"code": "VARCHAR"}
    assert catalog.columns_by_uid["source.shop.raw.payments"] == {
        "amount": "DECIMAL",
        "currency": "VARCHAR",
    }


def test_merge_fills_undocumented_leaf_columns() -> None:
    source = _node("source.shop.raw.payments", kind=ResourceType.SOURCE, columns={})
    manifest = Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={source.unique_id: source}
    )
    catalog = Catalog.from_raw(
        _raw_catalog(
            nodes={},
            sources={
                "source.shop.raw.payments": _catalog_entry(
                    "source.shop.raw.payments", "raw", "payments", amount="DECIMAL"
                )
            },
        )
    )
    merged = manifest.merge_catalog(catalog)
    cols = merged.nodes["source.shop.raw.payments"].columns
    assert set(cols) == {"amount"}
    assert cols["amount"].data_type == "DECIMAL"


def test_documented_columns_win_on_conflict() -> None:
    # The human documented `amount` with a description; the catalog reports a
    # different type for the same column. The documented Column is kept intact.
    source = _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        columns={"amount": Column(name="amount", data_type="NUMERIC(38,9)", description="cents")},
    )
    manifest = Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={source.unique_id: source}
    )
    catalog = Catalog.from_raw(
        _raw_catalog(
            nodes={},
            sources={
                "source.shop.raw.payments": _catalog_entry(
                    "source.shop.raw.payments",
                    "raw",
                    "payments",
                    AMOUNT="FLOAT",
                    currency="VARCHAR",
                )
            },
        )
    )
    merged = manifest.merge_catalog(catalog)
    cols = merged.nodes["source.shop.raw.payments"].columns
    # `amount` keeps its documented type and description (catalog AMOUNT is the
    # same column, case-folded, so it does not duplicate); `currency` is added.
    assert cols["amount"].data_type == "NUMERIC(38,9)"
    assert cols["amount"].description == "cents"
    assert "AMOUNT" not in cols
    assert cols["currency"].data_type == "VARCHAR"


def _sql_node(uid: str, *, kind: ResourceType, sql: str | None, columns: dict[str, Column]) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=kind,
        fqn=tuple(uid.split(".")[1:]),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=f"models/{uid.split('.')[-1]}.sql",
        columns=columns,
    )


def test_catalog_lets_select_star_over_an_undocumented_source_resolve() -> None:
    # A source with no documented columns and a model that `SELECT *`s it: without
    # the catalog the star cannot expand and the model has no output columns; with
    # the catalog supplying the source's columns, they flow through.
    source = _node("source.shop.raw.payments", kind=ResourceType.SOURCE, columns={})
    model = _sql_node(
        "model.shop.stg_payments",
        kind=ResourceType.MODEL,
        sql="SELECT * FROM payments",
        columns={},
    )
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={source.unique_id: source, model.unique_id: model},
    )

    def model_columns(m: Manifest) -> set[str]:
        graph = build_manifest_graph(m).graph
        return {
            ref.column
            for ref in graph.subjects()
            if ref.source.kind is SourceKind.MODEL
            and ref.source.unique_id == "model.shop.stg_payments"
        }

    assert model_columns(manifest) == set()

    catalog = Catalog.from_raw(
        _raw_catalog(
            nodes={},
            sources={
                "source.shop.raw.payments": _catalog_entry(
                    "source.shop.raw.payments",
                    "raw",
                    "payments",
                    amount="DECIMAL",
                    currency="VARCHAR",
                )
            },
        )
    )
    assert model_columns(manifest.merge_catalog(catalog)) == {"amount", "currency"}


def test_nodes_absent_from_catalog_pass_through_untouched() -> None:
    model = _node(
        "model.shop.orders",
        kind=ResourceType.MODEL,
        columns={"id": Column(name="id", data_type="INT", description=None)},
    )
    manifest = Manifest(schema_version="v12", adapter_type="duckdb", nodes={model.unique_id: model})
    merged = manifest.merge_catalog(Catalog.from_raw(_raw_catalog(nodes={}, sources={})))
    assert merged.nodes["model.shop.orders"] is manifest.nodes["model.shop.orders"]
