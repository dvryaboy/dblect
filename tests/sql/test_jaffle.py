"""Run the SQL static analyser against jaffle's vendored models.

The asserts are concrete: the NULL-group risk in `customers.sql` must
surface, and the staging models that don't exhibit it must stay quiet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlglot import Expr

from dblect.manifest import Manifest, Node, ResourceType
from dblect.sql import (
    FindingKind,
    detect_null_group_after_outer_join,
    parse_sql,
    scan_all,
)


@pytest.fixture(scope="module")
def jaffle(jaffle_manifest_path: Path) -> Manifest:
    return Manifest.from_file(jaffle_manifest_path)


def _models_with_code(manifest: Manifest) -> dict[str, Node]:
    return {
        uid: n
        for uid, n in manifest.nodes.items()
        if n.resource_type is ResourceType.MODEL and n.compiled_code is not None
    }


def _parsed(node: Node) -> Expr:
    assert node.compiled_code is not None
    return parse_sql(node.compiled_code, dialect="duckdb")


def test_customers_model_flags_null_group_risk(jaffle: Manifest) -> None:
    node = jaffle.nodes["model.jaffle_shop.customers"]
    findings = detect_null_group_after_outer_join(_parsed(node))
    assert any(
        f.kind is FindingKind.NULL_GROUP_AFTER_OUTER_JOIN and "orders.customer_id" in f.message
        for f in findings
    ), findings


def test_staging_models_have_no_null_group_findings(jaffle: Manifest) -> None:
    for name in ("stg_customers", "stg_orders", "stg_payments"):
        node = jaffle.nodes[f"model.jaffle_shop.{name}"]
        assert detect_null_group_after_outer_join(_parsed(node)) == ()


def test_orders_model_does_not_false_positive_on_null_group(jaffle: Manifest) -> None:
    # orders.sql joins order_payments (an aggregated CTE) back onto orders by order_id
    # and groups by order_id. order_id is the LEFT side's column, so no NULL-group risk.
    node = jaffle.nodes["model.jaffle_shop.orders"]
    findings = detect_null_group_after_outer_join(_parsed(node))
    assert findings == ()


def test_scan_all_surfaces_jaffle_null_group(jaffle: Manifest) -> None:
    kinds: set[FindingKind] = set()
    for node in _models_with_code(jaffle).values():
        kinds.update(f.kind for f in scan_all(_parsed(node)))
    assert FindingKind.NULL_GROUP_AFTER_OUTER_JOIN in kinds
