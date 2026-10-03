"""The value-domain lattice: a column's closed, non-null value set.

``Unbounded`` makes no claim (the top); ``Bounded(values)`` is a known set, and
a smaller set is a more precise claim, so two claims combine by intersection
(meet) and values from two branches combine by union (join). The empty set is
an ordinary ``Bounded`` value (the bottom): a column that is always NULL lands
there. The shared :func:`assert_lattice_laws`/:func:`assert_consistency_laws`
pin the bounded-lattice algebra generically; the examples below pin only the
readings specific to this lattice.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.predicate import Lit, LitKind
from dblect.lineage.properties.value_domain import (
    UNBOUNDED,
    VALUE_DOMAIN_LATTICE,
    Bounded,
    ValueDomain,
)
from tests.lineage._lattice_laws import assert_consistency_laws, assert_lattice_laws

_lits: st.SearchStrategy[Lit] = st.one_of(
    st.sampled_from(("shipped", "pending", "cancelled", "delivered")).map(
        lambda s: Lit(LitKind.STR, s)
    ),
    st.integers(-2, 2).map(lambda n: Lit(LitKind.NUM, Decimal(n))),
)
# Bounded sets drawn small, including the empty set, so the bottom
# participates in the law checks as often as any other value.
_bounded: st.SearchStrategy[ValueDomain] = st.frozensets(_lits, max_size=4).map(Bounded)
_values: st.SearchStrategy[ValueDomain] = st.one_of(_bounded, st.just(UNBOUNDED))


@given(_values, _values, _values)
def test_value_domain_lattice_laws(a: ValueDomain, b: ValueDomain, c: ValueDomain) -> None:
    assert_lattice_laws(VALUE_DOMAIN_LATTICE, a, b, c)


@given(_values, _values)
def test_value_domain_consistency_laws(declared: ValueDomain, value: ValueDomain) -> None:
    assert_consistency_laws(VALUE_DOMAIN_LATTICE, declared, value)


def test_top_is_unbounded() -> None:
    assert VALUE_DOMAIN_LATTICE.top is UNBOUNDED


def test_bottom_is_the_empty_bounded_set() -> None:
    """The empty set is an ordinary value here, not a contradiction marker: a
    column that is always NULL has no non-null values at all."""
    assert VALUE_DOMAIN_LATTICE.bottom == Bounded(frozenset())


def test_meet_is_intersection() -> None:
    a = Bounded(frozenset({Lit(LitKind.STR, "shipped"), Lit(LitKind.STR, "pending")}))
    b = Bounded(frozenset({Lit(LitKind.STR, "pending"), Lit(LitKind.STR, "cancelled")}))
    assert VALUE_DOMAIN_LATTICE.meet(a, b) == Bounded(frozenset({Lit(LitKind.STR, "pending")}))


def test_join_is_union() -> None:
    a = Bounded(frozenset({Lit(LitKind.STR, "shipped")}))
    b = Bounded(frozenset({Lit(LitKind.STR, "pending")}))
    assert VALUE_DOMAIN_LATTICE.join(a, b) == Bounded(
        frozenset({Lit(LitKind.STR, "shipped"), Lit(LitKind.STR, "pending")})
    )


def test_a_string_and_a_numeric_literal_of_the_same_text_stay_distinct() -> None:
    """Reusing ``Lit`` (rather than bare Python strings) is what keeps ``1`` and
    ``'1'`` from silently colliding in the same set."""
    numeric_one = Bounded(frozenset({Lit(LitKind.NUM, Decimal(1))}))
    string_one = Bounded(frozenset({Lit(LitKind.STR, "1")}))
    assert numeric_one != string_one
    assert VALUE_DOMAIN_LATTICE.meet(numeric_one, string_one) == Bounded(frozenset())
