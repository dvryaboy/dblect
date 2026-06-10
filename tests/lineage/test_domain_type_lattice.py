"""The domain-type lattice and the dimensional group underneath it.

Tag knowledge orders by precision: ``NAKED`` (no claim) is the top, a known tagging
refines it, and two disagreeing taggings meet to ``CONFLICT``. ``meet`` composes
agreeing tags and conflicts on disagreement; ``join`` keeps only what both sides
agree on. The shared :func:`assert_lattice_laws` pins the bounded-lattice algebra;
the targeted tests pin the domain-type reading of meet and join, and the dimension
tests pin the free-abelian-group arithmetic ``*`` and ``/`` ride on.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.facts.lattice import resolve
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    CONFLICT,
    DOMAIN_TYPE_LATTICE,
    NAKED,
    Concrete,
    Dimension,
    DomainTag,
    Nominal,
    PerRow,
    Tagged,
    Unit,
    tagged,
)
from tests.lineage._lattice_laws import assert_consistency_laws, assert_lattice_laws

_REL = SourceRef(SourceKind.MODEL, "model.shop.charges")
_CURRENCY_COL = ColumnRef(_REL, "currency")
_COUNTRY_COL = ColumnRef(_REL, "country")

# A small unit alphabet: two pinned currencies and one per-row companion column.
_units: st.SearchStrategy[Unit] = st.one_of(
    st.just(Concrete("usd")),
    st.just(Concrete("eur")),
    st.just(PerRow(_CURRENCY_COL)),
)
_nominal_bindings: st.SearchStrategy[Nominal] = st.one_of(
    st.just(Concrete("true")),
    st.just(Concrete("false")),
    st.just(PerRow(_COUNTRY_COL)),
)


@st.composite
def _dimensions(draw: st.DrawFn) -> Dimension:
    raw: dict[Unit, int] = draw(st.dictionaries(_units, st.integers(-2, 2), max_size=3))
    dim = Dimension.dimensionless()
    for unit, power in raw.items():
        dim = dim.multiply(Dimension.of(unit, power))
    return dim


@st.composite
def _tagged(draw: st.DrawFn) -> Tagged:
    dim: Dimension | None = draw(st.none() | _dimensions())
    nominal: dict[str, Nominal] = draw(
        st.dictionaries(st.sampled_from(("contains_tax", "country")), _nominal_bindings, max_size=2)
    )
    return tagged(dimension=dim, nominal=nominal)


# Mostly real taggings, occasionally the conflict bottom, so the law arms that
# touch bottom are exercised without swamping the normal cases.
_values: st.SearchStrategy[DomainTag] = st.one_of(_tagged(), st.just(CONFLICT))


@given(_values, _values, _values)
def test_domain_type_lattice_laws(a: DomainTag, b: DomainTag, c: DomainTag) -> None:
    assert_lattice_laws(DOMAIN_TYPE_LATTICE, a, b, c)


@given(_values, _values)
def test_domain_type_consistency_laws(declared: DomainTag, value: DomainTag) -> None:
    assert_consistency_laws(DOMAIN_TYPE_LATTICE, declared, value)


def test_top_is_naked() -> None:
    assert DOMAIN_TYPE_LATTICE.top == NAKED
    assert Tagged(dimension=None, nominal=frozenset()) == NAKED


def test_meet_composes_agreeing_tags() -> None:
    """A taxed-revenue claim met with a shipped-revenue claim carries both, the
    ``compose`` operation of the algebra."""
    taxed = tagged(nominal={"contains_tax": Concrete("false")})
    shipped = tagged(nominal={"contains_shipping": Concrete("true")})
    both = DOMAIN_TYPE_LATTICE.meet(taxed, shipped)
    assert both == tagged(
        nominal={"contains_tax": Concrete("false"), "contains_shipping": Concrete("true")}
    )


def test_meet_of_disagreeing_currencies_is_conflict() -> None:
    usd = tagged(dimension=Dimension.of(Concrete("usd")))
    eur = tagged(dimension=Dimension.of(Concrete("eur")))
    assert DOMAIN_TYPE_LATTICE.meet(usd, eur) is CONFLICT


def test_meet_of_disagreeing_nominal_is_conflict() -> None:
    taxed = tagged(nominal={"contains_tax": Concrete("true")})
    untaxed = tagged(nominal={"contains_tax": Concrete("false")})
    assert DOMAIN_TYPE_LATTICE.meet(taxed, untaxed) is CONFLICT


def test_meet_with_naked_keeps_the_known_tag() -> None:
    usd = tagged(dimension=Dimension.of(Concrete("usd")))
    assert DOMAIN_TYPE_LATTICE.meet(usd, NAKED) == usd
    assert DOMAIN_TYPE_LATTICE.meet(NAKED, usd) == usd


def test_join_widens_disagreement_to_naked() -> None:
    """Two known currencies at a confluence widen to ``NAKED``: the result is
    summable-by-omission only because the analyzer no longer knows the unit."""
    usd = tagged(dimension=Dimension.of(Concrete("usd")))
    eur = tagged(dimension=Dimension.of(Concrete("eur")))
    assert DOMAIN_TYPE_LATTICE.join(usd, eur) == NAKED


def test_join_keeps_the_tags_both_sides_share() -> None:
    a = tagged(
        dimension=Dimension.of(Concrete("usd")),
        nominal={"contains_tax": Concrete("false"), "country": PerRow(_COUNTRY_COL)},
    )
    b = tagged(
        dimension=Dimension.of(Concrete("usd")),
        nominal={"contains_tax": Concrete("true"), "country": PerRow(_COUNTRY_COL)},
    )
    joined = DOMAIN_TYPE_LATTICE.join(a, b)
    assert joined == tagged(
        dimension=Dimension.of(Concrete("usd")), nominal={"country": PerRow(_COUNTRY_COL)}
    )


def test_known_tag_refines_naked() -> None:
    usd = tagged(dimension=Dimension.of(Concrete("usd")))
    assert DOMAIN_TYPE_LATTICE.refines(usd, NAKED)
    assert not DOMAIN_TYPE_LATTICE.refines(NAKED, usd)


def test_resolution_detects_a_currency_contradiction() -> None:
    """Two pinned-currency facts on one column are mutually unsatisfiable, the
    contradiction grounding reports rather than silently picking one."""
    usd: Fact[DomainTag, ColumnRef] = Fact(
        scope=_CURRENCY_COL,
        value=tagged(dimension=Dimension.of(Concrete("usd"))),
        provenance=Declared(DeclaredSource.USER_ASSERTED),
    )
    eur: Fact[DomainTag, ColumnRef] = Fact(
        scope=_CURRENCY_COL,
        value=tagged(dimension=Dimension.of(Concrete("eur"))),
        provenance=Declared(DeclaredSource.USER_ASSERTED),
    )
    value, is_contradiction = resolve(DOMAIN_TYPE_LATTICE, (usd, eur))
    assert is_contradiction
    assert value is CONFLICT


# --- the dimensional group ---------------------------------------------------


@given(_dimensions(), _dimensions(), _dimensions())
def test_dimension_multiply_is_a_commutative_monoid(
    a: Dimension, b: Dimension, c: Dimension
) -> None:
    assert a.multiply(b) == b.multiply(a)
    assert a.multiply(b).multiply(c) == a.multiply(b.multiply(c))
    assert a.multiply(Dimension.dimensionless()) == a


@given(_dimensions(), _dimensions())
def test_dimension_divide_inverts_multiply(a: Dimension, b: Dimension) -> None:
    assert a.multiply(b).divide(b) == a


def test_same_currency_ratio_cancels_to_dimensionless() -> None:
    usd = Dimension.of(Concrete("usd"))
    assert usd.divide(usd).is_dimensionless


def test_money_squared_is_a_known_point_not_dimensionless() -> None:
    usd = Dimension.of(Concrete("usd"))
    squared = usd.multiply(usd)
    assert squared == Dimension.of(Concrete("usd"), 2)
    assert not squared.is_dimensionless


def test_per_row_units_cancel_only_for_the_same_column() -> None:
    other = ColumnRef(_REL, "settlement_currency")
    same = Dimension.of(PerRow(_CURRENCY_COL))
    assert same.divide(same).is_dimensionless
    mixed = Dimension.of(PerRow(_CURRENCY_COL)).divide(Dimension.of(PerRow(other)))
    assert not mixed.is_dimensionless
