"""Reusable lattice-law assertions, shared by every property's lattice test.

The facts substrate derives ``resolve`` and ``consistent`` from a property's
:class:`Lattice`, so the lattice laws are the single place those behaviours are
pinned. Each property's test calls :func:`assert_lattice_laws` under its own
Hypothesis strategy; this module is not a test module itself (no ``test_``
prefix), so pytest does not collect it.
"""

from __future__ import annotations

from typing import TypeVar

from dblect.lineage.facts.lattice import Lattice, consistent

K = TypeVar("K")


def assert_lattice_laws(lat: Lattice[K], a: K, b: K, c: K) -> None:
    """Assert the bounded-lattice laws on three sample values of ``lat``."""
    meet, join = lat.meet, lat.join

    # Commutativity.
    assert meet(a, b) == meet(b, a)
    assert join(a, b) == join(b, a)
    # Associativity.
    assert meet(meet(a, b), c) == meet(a, meet(b, c))
    assert join(join(a, b), c) == join(a, join(b, c))
    # Idempotence.
    assert meet(a, a) == a
    assert join(a, a) == a
    # Absorption.
    assert meet(a, join(a, b)) == a
    assert join(a, meet(a, b)) == a
    # Bounded-lattice identities: top is the meet identity / join annihilator,
    # bottom is the join identity / meet annihilator.
    assert meet(lat.top, a) == a
    assert join(lat.top, a) == lat.top
    assert join(lat.bottom, a) == a
    assert meet(lat.bottom, a) == lat.bottom
    # refines is reflexive.
    assert lat.refines(a, a)


def assert_consistency_laws(lat: Lattice[K], declared: K, value: K) -> None:
    """Assert the derived ``consistent`` check honours the design contract:
    an opaque (top) inferred value never fails, a contradiction (bottom) always
    fails, and a value is consistent with itself unless it is bottom."""
    check = consistent(lat)
    # An opaque upstream never fails the check, for any declaration.
    assert check(declared, lat.top)
    # A derived contradiction is a finding, never a vacuous pass.
    assert not check(declared, lat.bottom)
    # Reflexivity off bottom: a value honours itself.
    if value != lat.bottom:
        assert check(value, value)
    # The check agrees with refines off the top/bottom arms.
    if value not in (lat.top, lat.bottom):
        assert check(declared, value) == lat.refines(value, declared)
