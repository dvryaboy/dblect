# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportAssignmentType=false
"""The fact bridge's value-domain half: a bare enum scalar, or an open enum
facet on a domain type, grounds a closed member set on its column.

This closes #135's propagation gap (a bare ``NominalEnum``/``UnitEnum``
annotation was routed to a ``ScalarDecl`` that carried no fact) and #36's
contract-declared half (the accepted-values test discoverer, pinned in
``test_value_domain_facts.py``, is the zero-declaration companion): both are
the same ``ValueDomain`` fact, grounded from two different declaration
channels.
"""

from dblect.demo import Country, Currency, Money
from dblect.lineage.facts.model import Declared, DeclaredSource, by_scope
from dblect.lineage.facts.property import FactDiscoverer
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.predicate import Lit, LitKind
from dblect.lineage.properties.value_domain import Bounded, value_domain_grounding
from dblect.types import (
    Decimal,
    DomainType,
    ModelContract,
    NominalEnum,
    ResolvedContracts,
    contract_value_domain_discoverer,
    resolve_contracts,
)
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_CHARGES = _manifest(_node("model.shop.stg_charges"))
_CHARGES_SRC = SourceRef(SourceKind.MODEL, "model.shop.stg_charges")


def _resolved() -> ResolvedContracts:
    return resolve_contracts(_CHARGES)


def _str_set(*values: str) -> Bounded:
    return Bounded(frozenset(Lit(LitKind.STR, v) for v in values))


def _country_set() -> Bounded:
    return _str_set(*(member.value for member in Country))


def _currency_set() -> Bounded:
    return _str_set(*(member.value for member in Currency))


# --- a bare enum scalar (closes #135's ScalarDecl gap) -------------------------


def test_bare_nominal_enum_scalar_grounds_its_member_set() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        country: Country

    resolved = _resolved()
    (fact,) = resolved.value_domain_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "country")
    assert fact.value == _country_set()
    assert fact.provenance == Declared(DeclaredSource.USER_ASSERTED)


def test_bare_unit_enum_scalar_also_grounds_its_member_set() -> None:
    """A ``UnitEnum`` scalar (not riding inside a domain type's dimensional
    tag) is just as closed a vocabulary as a ``NominalEnum`` one."""

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        settlement_currency: Currency

    resolved = _resolved()
    (fact,) = resolved.value_domain_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "settlement_currency")
    assert fact.value == _currency_set()


def test_a_mixed_case_scalar_field_grounds_the_case_folded_column() -> None:
    """``ColumnRef`` column names are case-folded at every construction site,
    and the lineage keys a model's columns that way, so a contract field written
    the way the warehouse spells the column still meets its propagated scope."""

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        Country_Code: Country

    (fact,) = _resolved().value_domain_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "country_code")


def test_bare_bool_scalar_grounds_nothing() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        is_test_order: bool

    assert _resolved().value_domain_facts == ()


def test_bare_str_scalar_grounds_nothing() -> None:
    """A plain ``str`` column carries no closed set: only an enum names one."""

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        notes: str

    assert _resolved().value_domain_facts == ()


# --- an open enum facet on a domain type ---------------------------------------


def test_open_unit_enum_facet_grounds_its_companion_column() -> None:
    """``Money``'s ``currency`` field is a ``UnitEnum`` facet; left open (not
    fixed to a literal), it deserves the same closed-set grounding as a bare
    scalar."""

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        charge_amount: Money.columns(amount="charge_amount", currency="currency")

    resolved = _resolved()
    (fact,) = resolved.value_domain_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "currency")
    assert fact.value == _currency_set()
    assert fact.detail is not None
    assert "StgCharges.charge_amount" in fact.detail


def test_a_mixed_case_facet_column_grounds_the_case_folded_scope() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        charge_amount: Money.columns(amount="charge_amount", currency="Currency")

    (fact,) = _resolved().value_domain_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "currency")


def test_open_nominal_enum_facet_grounds_its_companion_column() -> None:
    class Delivery(DomainType):
        amount: Decimal
        destination: Country

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        payout: Delivery

    resolved = _resolved()
    (fact,) = resolved.value_domain_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "destination")
    assert fact.value == _country_set()


def test_a_magnitude_less_domain_type_still_grounds_its_enum_facet() -> None:
    """A domain type with no magnitude at all (so it grounds no ``DomainTag``)
    still has a real column behind its enum facet: the value-domain fact is
    orthogonal to whether the tag algebra found anything."""

    class Locale(DomainType):
        country: Country

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        locale: Locale

    resolved = _resolved()
    assert resolved.tag_facts == ()  # no magnitude: nothing for DomainTag
    (fact,) = resolved.value_domain_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "country")
    assert fact.value == _country_set()


def test_fixed_facet_grounds_no_value_domain_fact() -> None:
    """A fixed facet (``currency=Currency.USD``) pins a literal identity
    rather than binding a column: there is no companion column to ground."""

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        sale: Money(amount="sale_amount", currency=Currency.USD)

    resolved = _resolved()
    assert resolved.value_domain_facts == ()
    assert len(resolved.tag_facts) == 1  # the tag itself still grounds


# --- two declarations meeting at grounding -------------------------------------


def test_two_overlapping_declarations_on_one_column_meet_to_their_intersection() -> None:
    class Narrow(NominalEnum):
        US = "US"
        GB = "GB"

    class Wide(NominalEnum):
        US = "US"
        GB = "GB"
        DE = "DE"

    class First(ModelContract):
        dbt_model = "stg_charges"
        country: Narrow

    class Second(ModelContract):
        dbt_model = "stg_charges"
        country: Wide

    resolved = _resolved()
    assert len(resolved.value_domain_facts) == 2
    ground = value_domain_grounding(by_scope(resolved.value_domain_facts))
    value = ground(ColumnRef(_CHARGES_SRC, "country")).value
    assert value == _str_set("US", "GB")


# --- the discoverer satisfies the substrate protocol ---------------------------


def test_value_domain_discoverer_satisfies_the_substrate_protocol() -> None:
    assert isinstance(contract_value_domain_discoverer(), FactDiscoverer)
