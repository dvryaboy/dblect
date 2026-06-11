# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportIncompatibleVariableOverride=false
"""Declaration semantics of ``DomainType``: field collection and classification,
refinement (``refine`` / call-form), column binding (``columns``), and
extension by subclassing, including multiple inheritance.

These pin the authoring contract from ``docs/design/declaration-dsl.md``: a
class is read as a schema, never instantiated; refining and fixing-a-field are
the same move; combining facets is multiple inheritance with agreement
required where two bases fix the same field.
"""

import pytest

from dblect.demo import Country, Currency, Money
from dblect.types import (
    Date,
    Decimal,
    DomainType,
    DomainTypeError,
    FieldKind,
)


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
