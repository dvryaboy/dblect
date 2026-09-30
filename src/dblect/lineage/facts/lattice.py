"""The precision lattice, and the resolution and consistency checks derived from it.

A property states its order once, as a :class:`Lattice`. ``resolve`` (fold a
node's facts to the most precise value consistent with all of them) and
``consistent`` (does an inferred value honour a declaration?) are *functions* of
the lattice, not fields a property can override, so they cannot drift from the
order resolution uses.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from dblect.lineage.facts.model import Annotation, Fact, Opacity

K = TypeVar("K")


@dataclass(frozen=True, slots=True)
class Lattice(Generic[K]):
    """``meet`` is the greatest lower bound (the more precise value), ``join`` the
    least upper bound (used where two branches merge, e.g. a UNION). ``top`` is
    'no information'; ``bottom`` is 'contradiction', a value that no data can
    satisfy."""

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
    # A degenerate lattice (top == bottom) has no distinct contradiction state.
    return value, value == lat.bottom and lat.bottom != lat.top


def consistent(lat: Lattice[K]) -> Callable[[K, K], bool]:
    """Build the consistency check for ``lat``: the inferred value honours the
    declaration when the SQL revealed nothing (top) or proved something at least
    as precise.

    ``top`` is checked before ``bottom`` so a value of top (nothing was
    inferred) still passes even in a degenerate lattice where top and bottom
    coincide. ``bottom`` is then handled explicitly: since bottom refines
    every value, skipping this check would let an inferred contradiction pass
    by default instead of being reported as a finding.
    """

    def check(declared: K, inferred: K) -> bool:
        if inferred == lat.top:
            return True
        if inferred == lat.bottom:
            return False
        return lat.refines(inferred, declared)

    return check


def annotate_fold(lat: Lattice[K], value: K, kids: Collection[Annotation[K]]) -> Annotation[K]:
    """Wrap a transfer's already-folded ``value`` with the diagnostic bits derived
    from its inputs: a non-top result is CONCRETE, a top result inherits EXPLICIT
    from a declared opt-out among ``kids`` or is IMPLICIT, and ``provisional`` is
    the OR of the inputs'.

    Shared by every property whose transfer folds several children's values into
    one: the propagator's own generic multi-child fold, and a property's custom
    fold for a shape the generic one cannot handle alone (a domain tag's additive
    combine, a ``CASE``'s THEN/ELSE union).
    """
    provisional = any(k.provisional for k in kids)
    if value != lat.top:
        return Annotation(value, Opacity.CONCRETE, provisional=provisional)
    explicit = any(k.opacity is Opacity.EXPLICIT for k in kids)
    opacity = Opacity.EXPLICIT if explicit else Opacity.IMPLICIT
    return Annotation(value, opacity, provisional=provisional)
