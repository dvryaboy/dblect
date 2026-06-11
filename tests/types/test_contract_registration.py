# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportAssignmentType=false
"""``ModelContract`` registration: defining a class is the declaration.

Classes register on definition through ``__init_subclass__`` into the active
registry; a class without its own ``dbt_model`` is an abstract base whose
declarations flow to concrete subclasses. ``Field(...)`` carries the
constraint vocabulary and inline fixings at one binding site.
"""

import pytest

from dblect.demo import Country, Currency, Money
from dblect.types import (
    ContractRegistry,
    Date,
    DomainDecl,
    DomainTypeError,
    Field,
    FieldKind,
    ForeignKey,
    ForeignKeyDecl,
    ModelContract,
    PrimaryKey,
    PrimaryKeyDecl,
    ScalarDecl,
    active_registry,
    isolated_registry,
)


def test_defining_a_contract_registers_it(registry: ContractRegistry) -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        charge_amount: Money.columns(amount="charge_amount", currency="currency")

    (spec,) = registry.contracts
    assert spec.dbt_model == "stg_charges"
    assert spec.name.endswith("StgCharges")
    decl = spec.declarations["charge_amount"].form
    assert isinstance(decl, DomainDecl)
    assert decl.spec.columns == {"amount": "charge_amount", "currency": "currency"}


def test_a_class_without_dbt_model_is_an_abstract_base(registry: ContractRegistry) -> None:
    class HasAuditColumns(ModelContract):
        loaded_at: Date

    assert registry.contracts == ()

    class DimUsers(HasAuditColumns):
        dbt_model = "dim_users"
        country: Country

    (spec,) = registry.contracts
    assert set(spec.declarations) == {"loaded_at", "country"}


def test_scalar_marker_and_foreign_key_declarations(registry: ContractRegistry) -> None:
    class FctOrders(ModelContract):
        dbt_model = "fct_orders"
        order_id: PrimaryKey
        customer_id: ForeignKey("dim_customers.customer_id")
        currency: Currency

    (spec,) = registry.contracts
    assert isinstance(spec.declarations["order_id"].form, PrimaryKeyDecl)
    fk = spec.declarations["customer_id"].form
    assert isinstance(fk, ForeignKeyDecl)
    assert fk.target == "dim_customers.customer_id"
    scalar = spec.declarations["currency"].form
    assert isinstance(scalar, ScalarDecl)
    assert scalar.type.kind is FieldKind.UNIT


def test_field_constraints_are_captured(registry: ContractRegistry) -> None:
    class FctOrders(ModelContract):
        dbt_model = "fct_orders"
        order_total: Money(amount="order_total", currency=Currency.USD) = Field(ge=0)

    (spec,) = registry.contracts
    constraints = spec.declarations["order_total"].constraints
    assert constraints is not None
    assert constraints.ge == 0


def test_non_negative_is_an_alias_for_ge_zero(registry: ContractRegistry) -> None:
    class M(ModelContract):
        dbt_model = "m"
        amount: Money(currency=Currency.USD) = Field(non_negative=True)

    (spec,) = registry.contracts
    constraints = spec.declarations["amount"].constraints
    assert constraints is not None
    assert constraints.ge == 0


def test_field_inline_fixing_refines_the_declared_type(registry: ContractRegistry) -> None:
    class Revenue(Money):
        contains_tax: bool
        contains_discount: bool

    class M(ModelContract):
        dbt_model = "m"
        discounted: Revenue = Field(contains_tax=False, contains_discount=True)

    (spec,) = registry.contracts
    form = spec.declarations["discounted"].form
    assert isinstance(form, DomainDecl)
    assert form.spec.fixed == {"contains_tax": False, "contains_discount": True}
    assert form.spec == Revenue.refine(contains_tax=False, contains_discount=True).spec()


def test_field_fixing_on_a_scalar_declaration_raises() -> None:
    with pytest.raises(DomainTypeError):

        class M(ModelContract):
            dbt_model = "m"
            country: Country = Field(contains_tax=False)


def test_two_contracts_may_describe_the_same_model(registry: ContractRegistry) -> None:
    class A(ModelContract):
        dbt_model = "m"
        order_id: PrimaryKey

    class B(ModelContract):
        dbt_model = "m"
        amount: Money(currency=Currency.USD)

    assert len(registry.contracts) == 2


def test_isolated_registry_does_not_leak_to_the_active_one(registry: ContractRegistry) -> None:
    with isolated_registry() as inner:

        class M(ModelContract):
            dbt_model = "m"
            order_id: PrimaryKey

        assert len(inner.contracts) == 1
        assert active_registry() is inner

    assert registry.contracts == ()
    assert active_registry() is registry
