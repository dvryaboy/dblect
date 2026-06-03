"""Semiring laws checked with Hypothesis.

For each concrete ``Semiring`` we verify the algebraic laws every property's
propagator silently assumes. A violation in any of these would let
``propagate`` produce results that depend on tree shape rather than data,
breaking property contracts in unobvious ways.

The "core" laws (commutativity, associativity, identity, distributivity) hold
for both strict semirings and the looser join-semilattice / set-union flavour
we use for where-provenance. The "strict" law (``0 x x = 0``) only applies to
strict semirings; ``UnionSemiring`` is a documented near-semiring that
doesn't satisfy it and the test suite reflects that.
"""

from __future__ import annotations

from typing import TypeVar

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.properties.aggregation_depth import MaxSemiring
from dblect.lineage.properties.nullability import Nullability, NullabilitySemiring
from dblect.lineage.semiring import BooleanSemiring, Semiring, UnionSemiring

K = TypeVar("K")


def _check_core_laws(sr: Semiring[K], values: tuple[K, K, K]) -> None:
    a, b, c = values
    # Commutativity of plus and times.
    assert sr.plus(a, b) == sr.plus(b, a)
    assert sr.times(a, b) == sr.times(b, a)
    # Associativity.
    assert sr.plus(sr.plus(a, b), c) == sr.plus(a, sr.plus(b, c))
    assert sr.times(sr.times(a, b), c) == sr.times(a, sr.times(b, c))
    # Identities.
    assert sr.plus(sr.zero, a) == a
    assert sr.times(sr.one, a) == a
    # Distributivity of times over plus.
    assert sr.times(a, sr.plus(b, c)) == sr.plus(sr.times(a, b), sr.times(a, c))


def _check_strict_absorption(sr: Semiring[K], a: K) -> None:
    """The strict-semiring extra law: ``0 x a == 0``."""
    assert sr.times(sr.zero, a) == sr.zero


def _check_is_near_not_strict(sr: Semiring[K], a: K) -> None:
    """A near-semiring has ``zero == one``, so ``times(zero, a) == a``, not ``zero``."""
    assert sr.times(sr.zero, a) == a


@given(st.tuples(st.booleans(), st.booleans(), st.booleans()))
def test_boolean_core_laws(values: tuple[bool, bool, bool]) -> None:
    _check_core_laws(BooleanSemiring(), values)


@given(st.booleans())
def test_boolean_strict_absorption(a: bool) -> None:
    _check_strict_absorption(BooleanSemiring(), a)


@given(
    st.tuples(
        st.frozensets(st.integers(min_value=-10, max_value=10)),
        st.frozensets(st.integers(min_value=-10, max_value=10)),
        st.frozensets(st.integers(min_value=-10, max_value=10)),
    )
)
def test_union_semiring_core_laws(
    values: tuple[frozenset[int], frozenset[int], frozenset[int]],
) -> None:
    _check_core_laws(UnionSemiring[int](), values)


def test_union_semiring_is_a_near_semiring_not_strict() -> None:
    """The set-union variant deliberately does not satisfy ``0 x a == 0``.

    ``zero`` equals ``one`` equals the empty set, and ``times`` is union.
    Pinning the non-strict behaviour keeps callers from accidentally relying
    on absorption when they pick this semiring.
    """
    sr = UnionSemiring[int]()
    a = frozenset({1, 2})
    assert sr.times(sr.zero, a) == a
    assert sr.times(sr.zero, a) != sr.zero


# MaxSemiring (aggregation-depth) is a near-semiring, like UnionSemiring.
_depths = st.integers(min_value=0, max_value=8)


@given(st.tuples(_depths, _depths, _depths))
def test_max_semiring_core_laws(values: tuple[int, int, int]) -> None:
    _check_core_laws(MaxSemiring(), values)


@given(_depths)
def test_max_semiring_is_a_near_semiring_not_strict(a: int) -> None:
    _check_is_near_not_strict(MaxSemiring(), a)


# NullabilitySemiring laws hold over the three operational values; CONTRADICTION
# never reaches the combine (see properties/nullability.py).
_taints = st.sampled_from([Nullability.NON_NULL, Nullability.NULLABLE, Nullability.UNKNOWN])


@given(st.tuples(_taints, _taints, _taints))
def test_nullability_semiring_core_laws(
    values: tuple[Nullability, Nullability, Nullability],
) -> None:
    _check_core_laws(NullabilitySemiring(), values)


@given(_taints)
def test_nullability_semiring_is_a_near_semiring_not_strict(a: Nullability) -> None:
    _check_is_near_not_strict(NullabilitySemiring(), a)


def test_protocol_runtime_check_passes_for_concrete_impls() -> None:
    """Every concrete semiring satisfies the ``Semiring`` protocol at runtime."""
    assert isinstance(BooleanSemiring(), Semiring)
    assert isinstance(UnionSemiring[int](), Semiring)
    assert isinstance(MaxSemiring(), Semiring)
    assert isinstance(NullabilitySemiring(), Semiring)
