"""Lattice, resolve, and consistent, checked against a concrete bounded lattice.

We use the subset lattice on a small universe (meet = intersection, join =
union, top = full set, bottom = empty set): a bona-fide bounded lattice that
exercises ``resolve`` (meet-fold, contradiction at bottom) and the derived
``consistent`` check independently of any shipping property. Per-property
lattices reuse :func:`assert_lattice_laws` under their own strategies.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.facts.lattice import Lattice, consistent, resolve
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from tests.lineage._lattice_laws import assert_consistency_laws, assert_lattice_laws

_UNIVERSE = frozenset({0, 1, 2, 3})


def _subset_lattice() -> Lattice[frozenset[int]]:
    return Lattice(
        meet=lambda a, b: a & b,
        join=lambda a, b: a | b,
        top=_UNIVERSE,
        bottom=frozenset(),
    )


_subsets = st.frozensets(st.integers(min_value=0, max_value=3))
_COL = ColumnRef(SourceRef(SourceKind.SOURCE, "source.shop.raw.orders"), "id")


def _fact(value: frozenset[int]) -> Fact[frozenset[int], ColumnRef]:
    return Fact(scope=_COL, value=value, provenance=Declared(DeclaredSource.DBT_GENERIC_TEST))


@given(_subsets, _subsets, _subsets)
def test_subset_lattice_laws(a: frozenset[int], b: frozenset[int], c: frozenset[int]) -> None:
    assert_lattice_laws(_subset_lattice(), a, b, c)


@given(_subsets, _subsets)
def test_subset_consistency_laws(declared: frozenset[int], value: frozenset[int]) -> None:
    assert_consistency_laws(_subset_lattice(), declared, value)


@given(st.lists(_subsets, max_size=6))
def test_resolve_is_order_independent(values: list[frozenset[int]]) -> None:
    """Resolution folds a bucket with meet; meet is associative and commutative,
    so any permutation of the same facts resolves to the same value."""
    lat = _subset_lattice()
    facts = tuple(_fact(v) for v in values)
    forward, _ = resolve(lat, facts)
    backward, _ = resolve(lat, tuple(reversed(facts)))
    assert forward == backward
    # The fold is exactly the intersection of every fact value (top when empty).
    expected = _UNIVERSE
    for v in values:
        expected = expected & v
    assert forward == expected


def test_resolve_flags_contradiction_at_bottom() -> None:
    """Two disjoint declarations meet to the empty set, the lattice bottom, which
    resolve reports as a contradiction while still returning the deterministic
    bottom value."""
    lat = _subset_lattice()
    facts = (_fact(frozenset({0, 1})), _fact(frozenset({2, 3})))
    value, is_contradiction = resolve(lat, facts)
    assert value == lat.bottom
    assert is_contradiction


def test_resolve_empty_bucket_is_top() -> None:
    lat = _subset_lattice()
    value, is_contradiction = resolve(lat, ())
    assert value == lat.top
    assert not is_contradiction


def test_consistent_handles_top_and_bottom() -> None:
    lat = _subset_lattice()
    check = consistent(lat)
    declared = frozenset({0, 1})
    # Opaque (top) inferred never fails.
    assert check(declared, lat.top)
    # A strictly more precise inferred value passes; a looser one does not.
    assert check(declared, frozenset({0}))
    assert not check(declared, frozenset({0, 1, 2}))
    # An inferred contradiction is a finding, not a vacuous pass.
    assert not check(declared, lat.bottom)


def _degenerate_lattice() -> Lattice[frozenset[int]]:
    """A nominal lattice with ``top == bottom``, the shape where-provenance and
    aggregation-depth carry."""
    empty: frozenset[int] = frozenset()
    return Lattice(meet=lambda a, b: a | b, join=lambda a, b: a & b, top=empty, bottom=empty)


def test_resolve_and_consistent_stay_sound_on_a_degenerate_lattice() -> None:
    """When ``top == bottom``, "no information" must not read as a contradiction:
    an empty bucket resolves to top without flagging a conflict, and an opaque
    inference still honours any declaration."""
    lat = _degenerate_lattice()
    value, is_contradiction = resolve(lat, ())
    assert value == lat.top
    assert not is_contradiction
    check = consistent(lat)
    assert check(frozenset({0, 1}), lat.top)
