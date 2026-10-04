# pyright: reportInvalidTypeForm=false, reportUnusedClass=false
"""Joins that equate keys of two different entities, inferred with no declaration.

A declared single-column key starts an entity where the key is a base column or an
unchanged copy of one and the SQL does not already make it unique. The entity follows
copies, and a foreign key puts the child column in the parent's entity. A join
equating two entities is flagged; a side with no entity stays quiet.

The project is a small jaffle shop: ``stg_customers`` and ``stg_orders`` rename source
ids and ``stg_orders.customer_id`` is tested against ``stg_customers``. Each row adds
the models it needs and the join it checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest
import sqlglot
import sqlglot.expressions as exp

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFindingKind, run_check
from dblect.manifest import Node, ResourceType
from dblect.severity import Severity, severity_of
from dblect.types import DomainType, ForeignKey, Integer, ModelContract, NominalEnum
from tests._manifest_builders import cols, manifest, node, relationships_test, unique_test
from tests._manifest_builders import unique_combination_test as unique_combination

_DUCKDB = profile_for_adapter("duckdb")
_ENTITY_MISMATCH = CheckFindingKind.JOIN_KEY_ENTITY_MISMATCH

_SOURCES = {
    "raw_customers": ("id", "name", "signed_up_at"),
    "raw_orders": ("id", "customer_id", "store_id", "ordered_at"),
    "customer_details": ("customer_id", "tier"),
    "player": ("id",),
    "player_attributes": ("id",),
    "app_users": ("id",),
}


def _m(name: str, sql: str, key: str | None = None) -> tuple[Node, ...]:
    """A model, with a ``unique`` test on ``key`` when given."""
    uid = f"model.shop.{name}"
    columns = cols(**dict.fromkeys(cast(exp.Query, sqlglot.parse_one(sql)).named_selects, "INT"))
    return (node(uid, sql, columns=columns), *((unique_test(uid, key),) if key else ()))


def _src(name: str, key: str | None = None) -> tuple[Node, ...]:
    uid = f"source.shop.raw.{name}"
    columns = cols(**dict.fromkeys(_SOURCES[name], "INT"))
    return (
        node(uid, kind=ResourceType.SOURCE, columns=columns),
        *((unique_test(uid, key),) if key else ()),
    )


_STG_CUSTOMERS = "model.shop.stg_customers"
_STG_ORDERS = "model.shop.stg_orders"
_DETAILS = "source.shop.raw.customer_details"


def _project(sql: str, *extra: Node) -> list[Node]:
    return [
        *(n for name in _SOURCES for n in _src(name)),
        *_m(
            "stg_customers",
            "SELECT id AS customer_id, signed_up_at FROM raw_customers",
            "customer_id",
        ),
        *_m(
            "stg_orders",
            "SELECT id AS order_id, customer_id, store_id, ordered_at FROM raw_orders",
            "order_id",
        ),
        relationships_test(_STG_ORDERS, "customer_id", _STG_CUSTOMERS, "customer_id"),
        *_m("daily_orders", "SELECT ordered_at FROM stg_orders GROUP BY ordered_at", "ordered_at"),
        *_m(
            "daily_signups",
            "SELECT signed_up_at FROM stg_customers GROUP BY signed_up_at",
            "signed_up_at",
        ),
        *_m("joined", sql),
        *extra,
    ]


def _join(left: str, right: str, on: str) -> str:
    return f"SELECT 1 AS x FROM {left} JOIN {right} ON {on}"


_OC = ("stg_orders o", "stg_customers c")
_OO = ("stg_orders o", "stg_orders o2")
_DETAILS_JOIN = _join("stg_customers c", "customer_details d", "c.customer_id = d.customer_id")
_IDS = "SELECT order_id AS id FROM stg_orders UNION ALL SELECT {} FROM stg_orders"
_IDS_JOIN = (
    f"WITH ids AS ({_IDS}) SELECT 1 AS x FROM ids JOIN stg_customers c ON ids.id = c.customer_id"
)
_STORE_JOIN = _join(*_OO, "o.store_id = o2.order_id")
_ROLLUP = "SELECT {} AS d FROM stg_orders GROUP BY 1"


@dataclass(frozen=True, slots=True)
class _Case:
    id: str
    sql: str
    flagged: bool
    extra: tuple[Node, ...] = ()


_CASES = (
    _Case("order id to customer id", _join(*_OC, "o.order_id = c.customer_id"), True),
    _Case("foreign key to its parent", _join(*_OC, "o.customer_id = c.customer_id"), False),
    _Case("foreign key to another entity", _join(*_OO, "o.customer_id = o2.order_id"), True),
    _Case("side with no entity", _join(*_OC, "o.store_id = c.customer_id"), False),
    _Case(
        "renamed through a CTE",
        "WITH oc AS (SELECT order_id AS oid FROM stg_orders) "
        "SELECT 1 AS x FROM oc JOIN stg_customers c ON oc.oid = c.customer_id",
        True,
    ),
    _Case(
        "source keys joined directly",
        _join("player_attributes a", "player p", "a.id = p.id"),
        True,
        (*_src("player", "id"), *_src("player_attributes", "id")),
    ),
    _Case(
        "keys made unique by grouping",
        _join("daily_orders o", "daily_signups s", "o.ordered_at = s.signed_up_at"),
        False,
    ),
    # A correct 1:1 join that nothing declares; the relationships test clears it.
    _Case(
        "shared key, no relationships test",
        _DETAILS_JOIN,
        True,
        (unique_test(_DETAILS, "customer_id"),),
    ),
    _Case(
        "shared key with a relationships test",
        _DETAILS_JOIN,
        False,
        (
            unique_test(_DETAILS, "customer_id"),
            relationships_test(_DETAILS, "customer_id", _STG_CUSTOMERS, "customer_id"),
        ),
    ),
    _Case(
        "composite key",
        _DETAILS_JOIN,
        False,
        (unique_combination(_DETAILS, "customer_id", "tier"),),
    ),
    _Case(
        "conditional key",
        _DETAILS_JOIN,
        False,
        (unique_test(_DETAILS, "customer_id", where="tier = 'gold'"),),
    ),
    _Case("union of one entity's keys", _IDS_JOIN.format("order_id"), True),
    _Case("union of two entities' keys", _IDS_JOIN.format("customer_id"), False),
    _Case(
        "nested unions of one entity's keys",
        _join("a_outer u", "stg_customers c", "u.id = c.customer_id"),
        True,
        (
            *_m("a_outer", "SELECT id FROM z_inner UNION ALL SELECT order_id FROM stg_orders"),
            *_m("z_inner", _IDS.format("order_id")),
        ),
    ),
    _Case(
        "computed key",
        _join("stg_orders o", "cleaned c", "o.customer_id = c.customer_id"),
        False,
        _m("cleaned", "SELECT id + 0 AS customer_id FROM raw_customers", "customer_id"),
    ),
    _Case(
        "coalesced key",
        _join("stg_orders o", "spine s", "o.customer_id = s.customer_id"),
        False,
        _m(
            "spine",
            "SELECT COALESCE(c.id, o.customer_id) AS customer_id "
            "FROM raw_customers c FULL JOIN raw_orders o ON c.id = o.customer_id",
            "customer_id",
        ),
    ),
    _Case(
        "union of sources declared unique",
        _join("all_users u", "raw_customers c", "u.id = c.id"),
        False,
        (
            *_m(
                "all_users", "SELECT id FROM raw_customers UNION ALL SELECT id FROM app_users", "id"
            ),
            *_src("raw_customers", "id"),
            *_src("app_users", "id"),
        ),
    ),
    _Case(
        "rollups grouped by a cast and a truncation",
        _join("daily_cast a", "daily_trunc b", "a.d = b.d"),
        False,
        (
            *_m("daily_cast", _ROLLUP.format("CAST(ordered_at AS DATE)"), "d"),
            *_m("daily_trunc", _ROLLUP.format("DATE_TRUNC('day', ordered_at)"), "d"),
        ),
    ),
    # A foreign key that holds only over some rows must not merge the entities.
    _Case(
        "conditional foreign key",
        _STORE_JOIN,
        False,
        (
            relationships_test(
                _STG_ORDERS, "store_id", _STG_CUSTOMERS, "customer_id", where="store_id < 10"
            ),
        ),
    ),
    # The union output takes the parent's entity; its entity-less arms do not.
    _Case(
        "foreign key on a union of entity-less columns",
        _STORE_JOIN,
        False,
        (
            *_m(
                "store_ids",
                "SELECT store_id AS id FROM stg_orders UNION ALL SELECT store_id FROM stg_orders",
            ),
            relationships_test("model.shop.store_ids", "id", _STG_CUSTOMERS, "customer_id"),
        ),
    ),
)


def _entity_findings(sql: str, *extra: Node) -> list[str]:
    report = run_check(manifest(*_project(sql, *extra)), _DUCKDB)
    assert report.unbuilt == ()
    return [f.message for f in report.findings if f.kind is _ENTITY_MISMATCH]


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
def test_join_across_inferred_entities(case: _Case) -> None:
    assert bool(_entity_findings(case.sql, *case.extra)) is case.flagged


def test_a_contract_foreign_key_gives_the_child_its_parents_entity() -> None:
    class StgOrders(ModelContract):
        dbt_model = "stg_orders"
        store_id: ForeignKey("stg_customers.customer_id")

    assert _entity_findings(_STORE_JOIN)


def test_the_finding_names_both_keys_and_the_fix() -> None:
    report = run_check(manifest(*_project(_join(*_OC, "o.order_id = c.customer_id"))), _DUCKDB)
    [finding] = [f for f in report.findings if f.kind is _ENTITY_MISMATCH]
    assert finding.model_unique_id == "model.shop.joined"
    assert severity_of(finding) is Severity.WARN
    for fragment in (
        "o.order_id = c.customer_id",
        "raw_orders.id",
        "raw_customers.id",
        "relationships",
    ):
        assert fragment in finding.message, finding.message


class _Entity(NominalEnum):
    CUSTOMER = "customer"
    ORDER = "order"


class _EntityId(DomainType):
    id: Integer
    entity: _Entity


def test_declared_entities_decide_over_inferred_ones() -> None:
    # Declared the same entity: the shared-key join the inference would flag is cleared.
    class StgCustomers(ModelContract):
        dbt_model = "stg_customers"
        customer_id: _EntityId.refine(entity=_Entity.CUSTOMER).columns(id="customer_id")

    class CustomerDetails(ModelContract):
        dbt_model = "customer_details"
        customer_id: _EntityId.refine(entity=_Entity.CUSTOMER).columns(id="customer_id")

    assert not _entity_findings(_DETAILS_JOIN, unique_test(_DETAILS, "customer_id"))


def test_a_declared_conflict_is_reported_once() -> None:
    class StgCustomers(ModelContract):
        dbt_model = "stg_customers"
        customer_id: _EntityId.refine(entity=_Entity.CUSTOMER).columns(id="customer_id")

    class StgOrders(ModelContract):
        dbt_model = "stg_orders"
        order_id: _EntityId.refine(entity=_Entity.ORDER).columns(id="order_id")

    report = run_check(manifest(*_project(_join(*_OC, "o.order_id = c.customer_id"))), _DUCKDB)
    kinds = [f.kind for f in report.findings]
    assert kinds.count(CheckFindingKind.JOIN_KEY_TYPE_MISMATCH) == 1
    assert _ENTITY_MISMATCH not in kinds
