# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance, so annotating it that way trips pyright's "self is a supertype of its
# class" rule; the proxy usage itself stays fully checked. Typed ``self`` access in
# authored contracts is the deferred generated-stubs concern.
"""``@contract`` methods resolved through the bridge into substrate facts.

A contract method that returns a fact feeds the substrate: ``determines`` becomes
a functional-dependency fact, ``key`` / ``grain`` candidate keys (merged with the
``PrimaryKey`` markers), and ``references`` a foreign-key edge. A method that
returns a predicate is collected for running, not grounded. Resolution runs after
the whole scan, so a column reference into another model is resolved against the
manifest and a cross-relation ``determines`` (which the substrate does not carry)
is a finding rather than a silent fact.
"""

from __future__ import annotations

from dblect.contracts import ContractSelf, ast, contract, models
from dblect.lineage.facts.grounding import collect
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.functional_dependency import FD, FDSet
from dblect.lineage.properties.uniqueness import CandidateKeySet
from dblect.manifest import Manifest, Node, ResourceType
from dblect.types import (
    ForeignKeyEdge,
    IssueCode,
    ModelContract,
    PrimaryKey,
    contract_fd_discoverer,
    resolve_contracts,
)

_CHARGES = SourceRef(SourceKind.MODEL, "model.shop.stg_charges")
_ORDERS = SourceRef(SourceKind.MODEL, "model.shop.fct_orders")
_ITEMS = SourceRef(SourceKind.MODEL, "model.shop.stg_order_items")


def _node(uid: str) -> Node:
    name = uid.split(".")[-1]
    return Node(
        unique_id=uid,
        name=name,
        resource_type=ResourceType.MODEL,
        fqn=("shop", name),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _manifest() -> Manifest:
    nodes = [_node(_CHARGES.unique_id), _node(_ORDERS.unique_id), _node(_ITEMS.unique_id)]
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def test_determines_becomes_an_fd_fact() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"

        @contract
        def country_sets_currency(self: ContractSelf) -> object:
            return self.country.determines(self.currency)

    resolved = resolve_contracts(_manifest())
    assert resolved.issues == ()
    assert {(f.scope, f.value) for f in resolved.fd_facts} == {
        (_CHARGES, FDSet.of(FD(frozenset({"country"}), "currency")))
    }


def test_key_and_grain_become_candidate_keys() -> None:
    class FctOrders(ModelContract):
        dbt_model = "fct_orders"

        @contract
        def one_row_per_order(self: ContractSelf) -> object:
            return self.grain(per=self.order_id)

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"

        @contract
        def composite(self: ContractSelf) -> object:
            return self.key(self.country, self.charge_date)

    resolved = resolve_contracts(_manifest())
    assert resolved.issues == ()
    keys = {(f.scope, f.value) for f in resolved.key_facts}
    assert (_ORDERS, CandidateKeySet.of(frozenset({"order_id"}))) in keys
    assert (_CHARGES, CandidateKeySet.of(frozenset({"country", "charge_date"}))) in keys


def test_key_marker_and_method_key_merge() -> None:
    class FctOrders(ModelContract):
        dbt_model = "fct_orders"
        order_id: PrimaryKey

        @contract
        def also_unique_on_external_id(self: ContractSelf) -> object:
            return self.key(self.external_id)

    resolved = resolve_contracts(_manifest())
    keys = {f.value for f in resolved.key_facts if f.scope == _ORDERS}
    assert CandidateKeySet.of(frozenset({"order_id"})) in keys
    assert CandidateKeySet.of(frozenset({"external_id"})) in keys


def test_references_becomes_a_foreign_key_edge() -> None:
    class StgOrderItems(ModelContract):
        dbt_model = "stg_order_items"

        @contract
        def items_belong_to_orders(self: ContractSelf) -> object:
            return self.order_id.references(models.fct_orders.order_id)

    resolved = resolve_contracts(_manifest())
    assert resolved.issues == ()
    assert resolved.foreign_keys == (
        ForeignKeyEdge(child=ColumnRef(_ITEMS, "order_id"), parent=ColumnRef(_ORDERS, "order_id")),
    )


def test_cross_relation_determines_is_a_finding() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"

        @contract
        def bad(self: ContractSelf) -> object:
            return models.fct_orders.country.determines(self.currency)

    resolved = resolve_contracts(_manifest())
    assert resolved.fd_facts == ()
    assert [i.code for i in resolved.issues] == [IssueCode.MALFORMED_DECLARATION]


def test_predicate_is_collected_not_grounded() -> None:
    class FctOrders(ModelContract):
        dbt_model = "fct_orders"

        @contract
        def total_reconciles(self: ContractSelf) -> object:
            return (
                self.order_total.sum().group_by(self.order_id)
                == models.stg_order_items.subtotal.sum().group_by(models.stg_order_items.order_id)
            ).within(0.01)

    resolved = resolve_contracts(_manifest())
    assert resolved.fd_facts == ()
    assert len(resolved.predicates) == 1
    pred = resolved.predicates[0]
    assert pred.contract.endswith("FctOrders")
    assert pred.owner == _ORDERS
    assert isinstance(pred.predicate, ast.Compare)


def test_contract_fd_discoverer_grounds_the_fd_property() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"

        @contract
        def country_sets_currency(self: ContractSelf) -> object:
            return self.country.determines(self.currency)

    facts = collect(_manifest(), (contract_fd_discoverer(),), name_to_source={})
    assert facts[_CHARGES][0].value == FDSet.of(FD(frozenset({"country"}), "currency"))
