"""Generic activation of conditional facts against a relation's accumulated filter.

A captured conditional fact holds only over the rows matching its predicate. It
activates at a scope whose flowed row filter (the predicate-flow property) *implies*
that predicate: the scope's rows are then a subset of the fact's rows, so a claim
that survives row removal (a candidate key, a ``NOT_NULL``) carries unconditionally.

This step owns no property. A property supplies its value-lattice ``meet`` so an
activated value folds into the scope's annotation the same way a declared one would:
uniqueness meets a promoted candidate key into its key set, nullability would meet a
promoted ``NOT_NULL``, and a future axis likewise.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from dblect.lineage.predicate import Canon, entails_atoms

K = TypeVar("K")


def activate(
    base: K,
    conditionals: Iterable[tuple[K, frozenset[Canon]]],
    flow_atoms: frozenset[Canon],
    meet: Callable[[K, K], K],
) -> K:
    """Fold every conditional value whose predicate ``flow_atoms`` implies into ``base``.

    Each conditional is a ``(value, predicate)`` pair; where the accumulated filter
    implies the predicate, the value joins ``base`` by ``meet``. A conditional whose
    predicate is not implied leaves ``base`` untouched, so activation only ever adds
    information and never over-claims.
    """
    result = base
    for value, predicate in conditionals:
        if entails_atoms(flow_atoms, predicate):
            result = meet(result, value)
    return result
