"""The candidate-key lattice for the uniqueness property.

Uniqueness orders by precision where knowing more keys is more precise: ``meet``
unions key sets (two declarations both hold), ``join`` intersects them (a
confluence keeps only shared keys), ``top`` is the empty key set, and ``bottom``
is a formal universal element no real resolution reaches. The shared
:func:`assert_lattice_laws` pins the bounded-lattice algebra; the targeted tests
pin the uniqueness-specific reading of meet, join, and refines, plus that
resolution never contradicts.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.facts.lattice import resolve
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.uniqueness import (
    ALL_KEYS,
    NO_KEYS,
    UNIQUENESS_LATTICE,
    CandidateKeySet,
    Key,
)
from tests.lineage._lattice_laws import assert_consistency_laws, assert_lattice_laws

_COLS = ("a", "b", "c")

_keys = st.frozensets(st.sampled_from(_COLS), max_size=3)
_key_sets = st.frozensets(_keys, max_size=4).map(CandidateKeySet)
# Mostly real key sets, occasionally the bottom sentinel, so the law arms that
# touch bottom are exercised without swamping the normal cases.
_values = st.one_of(_key_sets, st.just(ALL_KEYS))

_REL = SourceRef(SourceKind.MODEL, "model.shop.dim_customer")


def _key(*cols: str) -> Key:
    return frozenset(cols)


def _fact(value: CandidateKeySet) -> Fact[CandidateKeySet, SourceRef]:
    return Fact(scope=_REL, value=value, provenance=Declared(DeclaredSource.DBT_GENERIC_TEST))


@given(_values, _values, _values)
def test_uniqueness_lattice_laws(
    a: CandidateKeySet, b: CandidateKeySet, c: CandidateKeySet
) -> None:
    assert_lattice_laws(UNIQUENESS_LATTICE, a, b, c)


@given(_values, _values)
def test_uniqueness_consistency_laws(declared: CandidateKeySet, value: CandidateKeySet) -> None:
    assert_consistency_laws(UNIQUENESS_LATTICE, declared, value)


def test_top_is_no_keys() -> None:
    assert UNIQUENESS_LATTICE.top == NO_KEYS
    assert NO_KEYS.keys == frozenset()
    assert not NO_KEYS.is_bottom


def test_meet_unions_keys() -> None:
    """Resolving two single-column ``unique`` declarations keeps both as keys."""
    a = CandidateKeySet.of(_key("id"))
    b = CandidateKeySet.of(_key("email"))
    assert UNIQUENESS_LATTICE.meet(a, b) == CandidateKeySet.of(_key("id"), _key("email"))


def test_join_intersects_keys() -> None:
    """A confluence keeps only the keys both branches carry."""
    a = CandidateKeySet.of(_key("id"), _key("email"))
    b = CandidateKeySet.of(_key("id"))
    assert UNIQUENESS_LATTICE.join(a, b) == CandidateKeySet.of(_key("id"))


def test_more_keys_refines_fewer() -> None:
    fewer = CandidateKeySet.of(_key("id"))
    more = CandidateKeySet.of(_key("id"), _key("email"))
    assert UNIQUENESS_LATTICE.refines(more, fewer)
    assert not UNIQUENESS_LATTICE.refines(fewer, more)
    # Everything refines top (the empty key set).
    assert UNIQUENESS_LATTICE.refines(fewer, NO_KEYS)


@given(st.lists(_key_sets, max_size=6))
def test_resolution_never_contradicts(values: list[CandidateKeySet]) -> None:
    """Uniqueness declarations only ever union, so a bucket of real key sets
    resolves to their combined keys and never reports a contradiction."""
    facts = tuple(_fact(v) for v in values)
    value, is_contradiction = resolve(UNIQUENESS_LATTICE, facts)
    assert not is_contradiction
    expected: frozenset[Key] = frozenset()
    for v in values:
        expected = expected | v.keys
    assert value == CandidateKeySet(expected)
