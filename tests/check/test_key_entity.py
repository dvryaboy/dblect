# pyright: reportInvalidTypeForm=false, reportUnusedClass=false
"""Joins that equate keys of two different entities, inferred with no declaration.

A declared single-column key starts an entity where the SQL does not already make the
column unique. The entity follows the column through renames, CTEs, and model
boundaries, and a foreign key (a ``relationships`` test) puts the child column in the
parent's entity. A join equating two columns in different entities is flagged; a side
with no entity stays quiet.

The project below is a small jaffle shop: ``stg_customers`` and ``stg_orders`` rename
source ids, ``stg_orders.customer_id`` is tested against ``stg_customers``, and each
row varies only the model that joins them.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFindingKind, run_check
from dblect.manifest import Node, ResourceType
from dblect.severity import Severity, severity_of
from dblect.types import DomainType, ForeignKey, Integer, ModelContract, NominalEnum
from tests._manifest_builders import cols, manifest, node, relationships_test, unique_test
from tests._manifest_builders import unique_combination_test as unique_combination

_DUCKDB = profile_for_adapter("duckdb")

_ENTITY_MISMATCH = CheckFindingKind.JOIN_KEY_ENTITY_MISMATCH

_STG_CUSTOMERS = "model.shop.stg_customers"
_STG_ORDERS = "model.shop.stg_orders"
_DETAILS = "source.shop.raw.customer_details"
_PLAYER = "source.shop.raw.player"
_ATTRIBUTES = "source.shop.raw.player_attributes"
_DAILY_ORDERS = "model.shop.daily_orders"
_DAILY_SIGNUPS = "model.shop.daily_signups"
_JOINED = "model.shop.joined"
_APP_USERS = "source.shop.raw.app_users"


def _model(name: str, sql: str, **dtypes: str) -> Node:
    return node(f"model.shop.{name}", sql, columns=cols(**dtypes))


def _project(sql: str, *extra: Node) -> list[Node]:
    return [
        node(
            "source.shop.raw.raw_customers",
            kind=ResourceType.SOURCE,
            columns=cols(id="INT", name="VARCHAR", signed_up_at="DATE"),
        ),
        node(
            "source.shop.raw.raw_orders",
            kind=ResourceType.SOURCE,
            columns=cols(id="INT", customer_id="INT", store_id="INT", ordered_at="DATE"),
        ),
        node(
            _DETAILS,
            kind=ResourceType.SOURCE,
            columns=cols(customer_id="INT", tier="VARCHAR"),
        ),
        node(
            _PLAYER,
            kind=ResourceType.SOURCE,
            columns=cols(id="INT", player_api_id="INT"),
        ),
        node(
            _ATTRIBUTES,
            kind=ResourceType.SOURCE,
            columns=cols(id="INT", player_api_id="INT"),
        ),
        node(
            _STG_CUSTOMERS,
            "SELECT id AS customer_id, name, signed_up_at FROM raw_customers",
            columns=cols(customer_id="INT", name="VARCHAR", signed_up_at="DATE"),
        ),
        node(
            _STG_ORDERS,
            "SELECT id AS order_id, customer_id, store_id, ordered_at FROM raw_orders",
            columns=cols(order_id="INT", customer_id="INT", store_id="INT", ordered_at="DATE"),
        ),
        node(
            _DAILY_ORDERS,
            "SELECT ordered_at, COUNT(*) AS n FROM stg_orders GROUP BY ordered_at",
            columns=cols(ordered_at="DATE", n="INT"),
        ),
        node(
            _DAILY_SIGNUPS,
            "SELECT signed_up_at, COUNT(*) AS n FROM stg_customers GROUP BY signed_up_at",
            columns=cols(signed_up_at="DATE", n="INT"),
        ),
        unique_test(_STG_CUSTOMERS, "customer_id"),
        unique_test(_STG_ORDERS, "order_id"),
        relationships_test(_STG_ORDERS, "customer_id", _STG_CUSTOMERS, "customer_id"),
        unique_test(_DAILY_ORDERS, "ordered_at"),
        unique_test(_DAILY_SIGNUPS, "signed_up_at"),
        node(_JOINED, sql, columns=cols(x="INT")),
        *extra,
    ]


@dataclass(frozen=True, slots=True)
class _Case:
    id: str
    sql: str
    flagged: bool
    extra: tuple[Node, ...] = ()


_ORDERS_CUSTOMERS = "SELECT c.customer_id AS x FROM stg_orders AS o JOIN stg_customers AS c ON {}"
_CUSTOMERS_DETAILS = (
    "SELECT c.customer_id AS x FROM stg_customers AS c "
    "JOIN customer_details AS d ON c.customer_id = d.customer_id"
)

_CASES = (
    _Case("order id to customer id", _ORDERS_CUSTOMERS.format("o.order_id = c.customer_id"), True),
    _Case(
        "foreign key to its parent",
        _ORDERS_CUSTOMERS.format("o.customer_id = c.customer_id"),
        False,
    ),
    # The foreign key alone gives the non-unique child its parent's entity.
    _Case(
        "foreign key to a different entity",
        "SELECT o.order_id AS x FROM stg_orders AS o "
        "JOIN stg_orders AS o2 ON o.customer_id = o2.order_id",
        True,
    ),
    _Case(
        "renamed through a CTE",
        "WITH oc AS (SELECT order_id AS oid FROM stg_orders) "
        "SELECT c.customer_id AS x FROM oc JOIN stg_customers AS c ON oc.oid = c.customer_id",
        True,
    ),
    _Case(
        "source keys joined directly",
        "SELECT p.id AS x FROM player_attributes AS pa JOIN player AS p ON pa.id = p.id",
        True,
        (unique_test(_PLAYER, "id"), unique_test(_ATTRIBUTES, "id")),
    ),
    # A non-unique column with no foreign key has no entity, so it never conflicts.
    _Case("side with no entity", _ORDERS_CUSTOMERS.format("o.store_id = c.customer_id"), False),
    # Known false positive: a rollup grouped by a plain column is a copy of that column,
    # so its key is anchored at the base column, like any other natural key.
    _Case(
        "keys made unique by grouping a plain column",
        "SELECT o.n AS x FROM daily_orders AS o "
        "JOIN daily_signups AS s ON o.ordered_at = s.signed_up_at",
        True,
    ),
    # A table sharing its parent's key is a correct 1:1 join, but without a
    # relationships test nothing says so. The test clears it.
    _Case(
        "shared key, no relationships test",
        _CUSTOMERS_DETAILS,
        True,
        (unique_test(_DETAILS, "customer_id"),),
    ),
    _Case(
        "shared key with a relationships test",
        _CUSTOMERS_DETAILS,
        False,
        (
            unique_test(_DETAILS, "customer_id"),
            relationships_test(_DETAILS, "customer_id", _STG_CUSTOMERS, "customer_id"),
        ),
    ),
    # Only a single-column key that always holds identifies rows on its own.
    _Case(
        "composite key starts no entity",
        _CUSTOMERS_DETAILS,
        False,
        (unique_combination(_DETAILS, "customer_id", "tier"),),
    ),
    _Case(
        "conditional key starts no entity",
        _CUSTOMERS_DETAILS,
        False,
        (unique_test(_DETAILS, "customer_id", where="tier = 'gold'"),),
    ),
    _Case(
        "union of one entity's keys",
        "WITH ids AS (SELECT order_id AS id FROM stg_orders UNION ALL SELECT order_id FROM stg_orders) "
        "SELECT c.customer_id AS x FROM ids JOIN stg_customers AS c ON ids.id = c.customer_id",
        True,
    ),
    # The outer union can be reached before the inner one it reads.
    _Case(
        "nested unions of one entity's keys",
        "SELECT c.customer_id AS x FROM a_outer AS u JOIN stg_customers AS c ON u.id = c.customer_id",
        True,
        (
            _model(
                "a_outer",
                "SELECT id FROM z_inner UNION ALL SELECT order_id FROM stg_orders",
                id="INT",
            ),
            _model(
                "z_inner",
                "SELECT order_id AS id FROM stg_orders UNION ALL SELECT order_id FROM stg_orders",
                id="INT",
            ),
        ),
    ),
    # A union output with a foreign key gets the parent's entity; its entity-less arms do not.
    _Case(
        "foreign key on a union of entity-less columns",
        "SELECT o.order_id AS x FROM stg_orders AS o "
        "JOIN stg_orders AS o2 ON o.store_id = o2.order_id",
        False,
        (
            _model(
                "store_ids",
                "SELECT store_id AS id FROM stg_orders UNION ALL SELECT store_id FROM stg_orders",
                id="INT",
            ),
            relationships_test("model.shop.store_ids", "id", _STG_CUSTOMERS, "customer_id"),
        ),
    ),
    _Case(
        "union of two entities' keys",
        "WITH ids AS (SELECT order_id AS id FROM stg_orders UNION ALL SELECT customer_id FROM stg_orders) "
        "SELECT c.customer_id AS x FROM ids JOIN stg_customers AS c ON ids.id = c.customer_id",
        False,
    ),
    # A computed key is not a copy of anything, so it makes no claim about which
    # entity it identifies, even when it is declared unique.
    _Case(
        "key computed from another key",
        "SELECT o.customer_id AS x FROM stg_orders AS o "
        "JOIN cleaned_customers AS c ON o.customer_id = c.customer_id",
        False,
        (
            _model(
                "cleaned_customers",
                "SELECT id + 0 AS customer_id FROM raw_customers",
                customer_id="INT",
            ),
            unique_test("model.shop.cleaned_customers", "customer_id"),
        ),
    ),
    _Case(
        "coalesced key",
        "SELECT o.customer_id AS x FROM stg_orders AS o "
        "JOIN spine AS s ON o.customer_id = s.customer_id",
        False,
        (
            _model(
                "spine",
                "SELECT COALESCE(c.id, o.customer_id) AS customer_id "
                "FROM raw_customers AS c FULL JOIN raw_orders AS o ON c.id = o.customer_id",
                customer_id="INT",
            ),
            unique_test("model.shop.spine", "customer_id"),
        ),
    ),
    _Case(
        "union of sources, declared unique",
        "SELECT c.name AS x FROM all_users AS u JOIN raw_customers AS c ON u.id = c.id",
        False,
        (
            node(_APP_USERS, kind=ResourceType.SOURCE, columns=cols(id="INT")),
            _model(
                "all_users",
                "SELECT id FROM raw_customers UNION ALL SELECT id FROM app_users",
                id="INT",
            ),
            unique_test("model.shop.all_users", "id"),
            unique_test("source.shop.raw.raw_customers", "id"),
            unique_test(_APP_USERS, "id"),
        ),
    ),
    _Case(
        "rollups grouped by a cast and by a truncation",
        "SELECT o.n AS x FROM daily_cast AS o JOIN daily_trunc AS s ON o.d = s.d",
        False,
        (
            _model(
                "daily_cast",
                "SELECT CAST(ordered_at AS DATE) AS d, COUNT(*) AS n FROM stg_orders GROUP BY 1",
                d="DATE",
                n="INT",
            ),
            unique_test("model.shop.daily_cast", "d"),
            _model(
                "daily_trunc",
                "SELECT DATE_TRUNC('day', ordered_at) AS d, COUNT(*) AS n FROM stg_orders GROUP BY 1",
                d="DATE",
                n="INT",
            ),
            unique_test("model.shop.daily_trunc", "d"),
        ),
    ),
    # Deduplicating a model on its key leaves the key in the same entity.
    _Case(
        "key through a ROW_NUMBER dedup",
        "SELECT o.order_id AS x FROM stg_orders AS o JOIN latest AS l ON o.order_id = l.order_id",
        False,
        (
            _model(
                "latest",
                "SELECT order_id FROM (SELECT order_id, ROW_NUMBER() OVER "
                "(PARTITION BY order_id ORDER BY ordered_at) AS rn FROM stg_orders) WHERE rn = 1",
                order_id="INT",
            ),
            unique_test("model.shop.latest", "order_id"),
        ),
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
def test_join_across_inferred_entities(case: _Case) -> None:
    report = run_check(manifest(*_project(case.sql, *case.extra)), _DUCKDB)
    assert report.unbuilt == ()
    found = [f for f in report.findings if f.kind is _ENTITY_MISMATCH]
    assert bool(found) is case.flagged, [f.message for f in report.findings]


def test_a_contract_foreign_key_gives_the_child_its_parents_entity() -> None:
    class StgOrders(ModelContract):
        dbt_model = "stg_orders"
        store_id: ForeignKey("stg_customers.customer_id")

    sql = (
        "SELECT o.order_id AS x FROM stg_orders AS o "
        "JOIN stg_orders AS o2 ON o.store_id = o2.order_id"
    )
    report = run_check(manifest(*_project(sql)), _DUCKDB)
    assert [f for f in report.findings if f.kind is _ENTITY_MISMATCH]


def test_the_finding_names_both_keys_and_the_fix() -> None:
    report = run_check(
        manifest(*_project(_ORDERS_CUSTOMERS.format("o.order_id = c.customer_id"))), _DUCKDB
    )
    [finding] = [f for f in report.findings if f.kind is _ENTITY_MISMATCH]
    assert finding.model_unique_id == _JOINED
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

    report = run_check(
        manifest(*_project(_CUSTOMERS_DETAILS, unique_test(_DETAILS, "customer_id"))), _DUCKDB
    )
    assert not [f for f in report.findings if f.kind is _ENTITY_MISMATCH]


def test_a_declared_conflict_is_reported_once() -> None:
    class StgCustomers(ModelContract):
        dbt_model = "stg_customers"
        customer_id: _EntityId.refine(entity=_Entity.CUSTOMER).columns(id="customer_id")

    class StgOrders(ModelContract):
        dbt_model = "stg_orders"
        order_id: _EntityId.refine(entity=_Entity.ORDER).columns(id="order_id")

    report = run_check(
        manifest(*_project(_ORDERS_CUSTOMERS.format("o.order_id = c.customer_id"))), _DUCKDB
    )
    kinds = [f.kind for f in report.findings]
    assert kinds.count(CheckFindingKind.JOIN_KEY_TYPE_MISMATCH) == 1
    assert _ENTITY_MISMATCH not in kinds
