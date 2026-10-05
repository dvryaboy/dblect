# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportIncompatibleVariableOverride=false
"""Declaration semantics of ``DomainType``: field collection and classification,
refinement (``refine`` / call-form), column binding (``columns``), and
extension by subclassing, including multiple inheritance.

These pin the authoring contract from ``docs/design/declaration-dsl.md``: a
class is read as a schema, never instantiated; refining and fixing-a-field are
the same move; combining facets is multiple inheritance with agreement
required where two bases fix the same field.
"""

from datetime import datetime
from itertools import product
from typing import cast

import pytest

from dblect.demo import Country, Currency, Money
from dblect.types import (
    BigInt,
    Date,
    Decimal,
    DomainType,
    DomainTypeError,
    FieldKind,
    Float,
    Integer,
    Timestamp,
)
from dblect.types.domain import DomainTypeMeta
from dblect.types.scalars import classify


class Revenue(Money):
    """Money plus what-the-number-includes facets, the doc's running example."""

    contains_tax: bool
    contains_discount: bool


# --- field collection and classification --------------------------------------


def test_money_fields_classify_magnitude_and_unit() -> None:
    spec = Money.spec()
    assert set(spec.fields) == {"amount", "currency"}
    assert spec.fields["amount"].kind is FieldKind.MAGNITUDE
    assert spec.fields["currency"].kind is FieldKind.UNIT
    assert spec.fields["currency"].enum is Currency
    assert spec.fixed == {}
    assert spec.columns == {}


def test_bool_enum_str_and_date_classification() -> None:
    class Shipment(DomainType):
        weight: Decimal
        expedited: bool
        origin: Country
        carrier: str
        shipped_on: Date

    spec = Shipment.spec()
    assert spec.fields["weight"].kind is FieldKind.MAGNITUDE
    assert spec.fields["expedited"].kind is FieldKind.NOMINAL
    assert spec.fields["origin"].kind is FieldKind.NOMINAL
    assert spec.fields["origin"].enum is Country
    assert spec.fields["carrier"].kind is FieldKind.NOMINAL
    assert spec.fields["shipped_on"].kind is FieldKind.INERT


def test_parameterized_decimal_carries_precision_and_scale() -> None:
    class Price(DomainType):
        amount: Decimal(18, 2)
        currency: Currency

    field = Price.spec().fields["amount"]
    assert field.kind is FieldKind.MAGNITUDE
    assert (field.precision, field.scale) == (18, 2)


def test_float_is_a_magnitude() -> None:
    # A floating-point column is unambiguously a measure: floats are not used as
    # identifiers or calendar years, so both the builtin and the marker sum.
    class Reading(DomainType):
        celsius: float
        kelvin: Float

    spec = Reading.spec()
    assert spec.fields["celsius"].kind is FieldKind.MAGNITUDE
    assert spec.fields["kelvin"].kind is FieldKind.MAGNITUDE


def test_timestamp_is_inert_like_date() -> None:
    # Timestamp is the date-time sibling of Date: it carries no tag of its own.
    class Event(DomainType):
        occurred_at: Timestamp
        ingested_at: datetime

    spec = Event.spec()
    assert spec.fields["occurred_at"].kind is FieldKind.INERT
    assert spec.fields["ingested_at"].kind is FieldKind.INERT


def test_bare_integer_is_inert_lenient_default() -> None:
    # An integer is algebraically a quantity yet by role often an identifier or a
    # calendar year, so a bare int makes no domain claim. The lenient default
    # accepts it as opaque (INERT): not summable as a magnitude, no tag imposed.
    # A measure is spelled Count/Decimal; an id or year carries its domain type.
    # (Strict mode rejects bare int instead; tracked on the lenient/strict issue.)
    class Account(DomainType):
        user_id: int
        signup_year: Integer
        external_ref: BigInt

    spec = Account.spec()
    assert spec.fields["user_id"].kind is FieldKind.INERT
    assert spec.fields["signup_year"].kind is FieldKind.INERT
    assert spec.fields["external_ref"].kind is FieldKind.INERT


@pytest.mark.parametrize(
    ("integers", "has_unit", "has_magnitude"),
    list(product((0, 1, 2), (False, True), (False, True))),
)
def test_an_integer_is_the_magnitude_only_when_a_unit_qualifies_it_alone(
    integers: int, has_unit: bool, has_magnitude: bool
) -> None:
    # Every combination of integer count, unit presence and explicit magnitude
    # presence. A unit only qualifies a quantity, so a lone integer beside a unit
    # (and no Decimal/Count/Float) is that quantity. Two integers leave no way to
    # tell which is the quantity, and an explicit magnitude already is it, so
    # integers stay inert there; without a unit an integer is an id or a year.
    annotations: dict[str, object] = {f"n{i}": BigInt for i in range(integers)}
    if has_unit:
        annotations["currency"] = Currency
    if has_magnitude:
        annotations["amount"] = Decimal
    made = cast(
        "type[DomainType]", DomainTypeMeta("T", (DomainType,), {"__annotations__": annotations})
    )
    spec = made.spec()

    promoted = integers == 1 and has_unit and not has_magnitude
    expected = FieldKind.MAGNITUDE if promoted else FieldKind.INERT
    for i in range(integers):
        assert spec.fields[f"n{i}"].kind is expected


def test_an_identifier_with_a_nominal_enum_stays_inert() -> None:
    class EntityId(DomainType):
        id: Integer
        entity: Country

    assert EntityId.spec().fields["id"].kind is FieldKind.INERT


def test_a_subclass_adding_a_unit_promotes_an_inherited_integer() -> None:
    class Quantity(DomainType):
        amount: BigInt

    class Cents(Quantity):
        currency: Currency

    assert Quantity.spec().fields["amount"].kind is FieldKind.INERT
    assert Cents.spec().fields["amount"].kind is FieldKind.MAGNITUDE


def test_a_bare_integer_scalar_declaration_stays_inert() -> None:
    assert classify("n", BigInt).kind is FieldKind.INERT


def test_unsupported_annotation_is_an_authoring_error() -> None:
    with pytest.raises(DomainTypeError):

        class Bad(DomainType):
            amount: object


# --- refinement ----------------------------------------------------------------


def test_refine_fixes_a_field_and_leaves_the_base_open() -> None:
    money_usd = Money.refine(currency=Currency.USD)
    assert money_usd.spec().fixed == {"currency": Currency.USD}
    assert Money.spec().fixed == {}  # refinement never mutates the base
    assert money_usd.spec().fields == Money.spec().fields


def test_call_form_is_refine() -> None:
    assert Money(currency=Currency.USD).spec() == Money.refine(currency=Currency.USD).spec()


def test_in_domain_string_literal_is_the_enum_value() -> None:
    # StrEnum value equality makes the two spellings one spec.
    assert Money.refine(currency="USD").spec() == Money.refine(currency=Currency.USD).spec()


def test_out_of_domain_string_is_kept_for_the_finding_not_raised() -> None:
    # The literal is vouched and wrong; that surfaces as a finding at
    # resolution (see the bridge tests), never as an authoring crash.
    assert Money.refine(currency="ZZZ").spec().fixed == {"currency": "ZZZ"}


def test_refine_chains_cumulatively() -> None:
    net = Revenue.refine(contains_tax=False).refine(contains_discount=True)
    assert net.spec().fixed == {"contains_tax": False, "contains_discount": True}


def test_refine_unknown_field_raises() -> None:
    with pytest.raises(DomainTypeError):
        Money.refine(colour="red")


def test_refine_magnitude_to_a_literal_raises() -> None:
    with pytest.raises(DomainTypeError):
        Money.refine(amount=5)


def test_refine_bool_field_requires_a_bool() -> None:
    with pytest.raises(DomainTypeError):
        Revenue.refine(contains_tax="false")


def test_refine_with_a_member_of_the_wrong_enum_raises() -> None:
    with pytest.raises(DomainTypeError):
        Money.refine(currency=Country.US)


# --- column binding ------------------------------------------------------------


def test_columns_maps_fields_to_warehouse_columns() -> None:
    bound = Money.columns(amount="sale_amount", currency="currency_code")
    assert bound.spec().columns == {"amount": "sale_amount", "currency": "currency_code"}
    assert bound.spec().fixed == {}


def test_call_form_magnitude_string_is_a_column_mapping() -> None:
    sale = Money(amount="sale_amount", currency=Currency.USD)
    assert sale.spec().columns == {"amount": "sale_amount"}
    assert sale.spec().fixed == {"currency": Currency.USD}


def test_columns_rejects_non_string_values() -> None:
    with pytest.raises(DomainTypeError):
        Money.columns(amount=5)


def test_columns_rejects_unknown_fields() -> None:
    with pytest.raises(DomainTypeError):
        Money.columns(colour="c")


def test_columns_then_refine_compose() -> None:
    t = Money.columns(amount="net_amount").refine(currency=Currency.EUR)
    assert t.spec().columns == {"amount": "net_amount"}
    assert t.spec().fixed == {"currency": Currency.EUR}


# --- extension by subclassing ----------------------------------------------------


def test_subclass_adds_a_facet() -> None:
    class ShippedRevenue(Revenue):
        contains_shipping: bool = True

    spec = ShippedRevenue.spec()
    assert spec.fields["contains_shipping"].kind is FieldKind.NOMINAL
    assert spec.fixed == {"contains_shipping": True}
    assert "contains_shipping" not in Revenue.spec().fields


def test_subclass_fixes_an_inherited_facet() -> None:
    class TaxedRevenue(Revenue):
        contains_tax: bool = True

    assert TaxedRevenue.spec().fixed == {"contains_tax": True}


def test_subclass_fixing_is_refine() -> None:
    class TaxedRevenue(Revenue):
        contains_tax: bool = True

    assert TaxedRevenue.spec() == Revenue.refine(contains_tax=True).spec()


def test_multiple_inheritance_unions_facets() -> None:
    class TaxedRevenue(Revenue):
        contains_tax: bool = True

    class ShippedRevenue(Revenue):
        contains_shipping: bool = True

    class TaxedShippedRevenue(TaxedRevenue, ShippedRevenue):
        pass

    spec = TaxedShippedRevenue.spec()
    assert spec.fixed == {"contains_tax": True, "contains_shipping": True}
    assert set(spec.fields) >= {"amount", "currency", "contains_tax", "contains_shipping"}


def test_multiple_inheritance_disagreeing_fixings_raise() -> None:
    class TaxedRevenue(Revenue):
        contains_tax: bool = True

    class UntaxedRevenue(Revenue):
        contains_tax: bool = False

    with pytest.raises(DomainTypeError):

        class Impossible(TaxedRevenue, UntaxedRevenue):
            pass


def test_subclass_override_settles_a_base_disagreement() -> None:
    class TaxedRevenue(Revenue):
        contains_tax: bool = True

    class UntaxedRevenue(Revenue):
        contains_tax: bool = False

    class Settled(TaxedRevenue, UntaxedRevenue):
        contains_tax: bool = True

    assert Settled.spec().fixed["contains_tax"] is True


def test_changing_an_inherited_field_type_raises() -> None:
    with pytest.raises(DomainTypeError):

        class Bad(Money):
            currency: Country


# --- the class is a schema, not a value -----------------------------------------


def test_calling_a_domain_type_specializes_rather_than_instantiates() -> None:
    specialized = Money(currency=Currency.USD)
    assert isinstance(specialized, type)
    assert specialized.spec().fixed == {"currency": Currency.USD}
