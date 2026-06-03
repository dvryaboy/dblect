"""The precision lattice, and the resolution and consistency checks derived from it.

A property states its order once, as a :class:`Lattice`. ``resolve`` (fold a
node's facts to the most precise value consistent with all of them) and
``consistent`` (does an inferred value honour a declaration?) are *functions* of
the lattice, not fields a property can override, so they cannot drift from the
order resolution uses.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from dblect.lineage.facts.model import Fact

K = TypeVar("K")


@dataclass(frozen=True, slots=True)
class Lattice(Generic[K]):
    """``meet`` is the greatest lower bound (the more precise value), ``join`` the
    least upper bound (used at a confluence). ``top`` is 'no information'; ``bottom``
    is 'contradiction', a value that no data can satisfy."""

    meet: Callable[[K, K], K]
    join: Callable[[K, K], K]
    top: K
    bottom: K

    def refines(self, finer: K, coarser: K) -> bool:
        """``finer`` is at least as precise as ``coarser``: their meet is ``finer``."""
        return self.meet(finer, coarser) == finer


def resolve(lat: Lattice[K], facts: tuple[Fact[K, Any], ...]) -> tuple[K, bool]:
    """Fold every fact at one scope to the most precise value consistent with all
    of them, returning ``(value, is_contradiction)``.

    Meet is associative and commutative by the lattice laws, so the result does
    not depend on discoverer order. A result of ``bottom`` means the declarations
    are mutually unsatisfiable; the caller raises a ``FactConflictError`` and keeps this
    deterministic value so the run stays reproducible.
    """
    value = lat.top
    for f in facts:
        value = lat.meet(value, f.value)
    # A degenerate lattice (top == bottom, the inert bottom a nominal property
    # carries) has no distinct contradiction state, so the fold is always "no
    # information", never a conflict.
    return value, value == lat.bottom and lat.bottom != lat.top


def consistent(lat: Lattice[K]) -> Callable[[K, K], bool]:
    """Build the consistency check for ``lat``: the inferred value honours the
    declaration when the SQL revealed nothing (top) or proved something at least
    as precise.

    ``top`` is checked before ``bottom``: an opaque inference always honours a
    declaration, including on a degenerate lattice (``top == bottom``, the inert
    bottom a nominal property carries) where the two coincide. ``bottom`` is then
    handled explicitly rather than left to ``refines``: ``bottom`` refines every
    value, so without its own arm an inferred contradiction would pass vacuously.
    An inferred ``bottom`` means propagation derived a contradiction at this node,
    which is a finding, not a silent pass.
    """

    def check(declared: K, inferred: K) -> bool:
        if inferred == lat.top:
            return True
        if inferred == lat.bottom:
            return False
        return lat.refines(inferred, declared)

    return check
