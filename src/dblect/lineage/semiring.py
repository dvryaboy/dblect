"""Semiring abstraction for property propagation.

A commutative semiring ``(K, +, x, 0, 1)`` is the algebraic backbone for a
propagated property. Specific properties (where-provenance, uniqueness as
candidate keys, nullability, fanout, semantic tags) instantiate ``Semiring``
with the K most natural for that property.

Core laws every instance satisfies (checked in
``tests/lineage/test_semiring_laws.py``):

* ``(K, +, 0)`` is a commutative monoid: ``+`` is associative and commutative;
  ``0 + x = x`` for all ``x``.
* ``(K, x, 1)`` is a commutative monoid: ``x`` is associative and commutative;
  ``1 x x = x`` for all ``x``.
* ``x`` distributes over ``+``: ``a x (b + c) = (a x b) + (a x c)``.

Strict semirings additionally satisfy ``0 x x = 0`` (zero absorbs times).
The Boolean semiring is strict; the set-union near-semiring used by
where-provenance is not (in that instance, ``zero == one == empty set``).
Properties whose propagation relies on absorption (a property reasoning
about "join with nothing produces nothing") should pick a strict semiring;
those that only need confluence and cross combinators (where-provenance,
many security-label flows) can use the looser structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

K = TypeVar("K")
E = TypeVar("E")


@runtime_checkable
class Semiring(Protocol[K]):
    """Commutative semiring with the operations needed for K-relations propagation.

    ``plus`` reconciles values at confluence points (UNION ALL across branches).
    ``times`` combines values at cross points (the implicit cross product
    underlying every JOIN).

    ``zero`` and ``one`` are declared as read-only properties so that frozen
    dataclass implementations (the natural way to build a concrete semiring)
    satisfy the protocol cleanly.
    """

    @property
    def zero(self) -> K: ...

    @property
    def one(self) -> K: ...

    def plus(self, a: K, b: K) -> K:
        """Confluence combinator. Associative, commutative, identity ``zero``."""
        ...

    def times(self, a: K, b: K) -> K:
        """Cross combinator. Associative, commutative, identity ``one``, annihilated by ``zero``."""
        ...


@dataclass(frozen=True, slots=True)
class BooleanSemiring:
    """The Boolean semiring ``({F, T}, or, and, F, T)``.

    Useful for laws-testing the propagator and as a sanity reference. Set
    semantics over tuples is what this semiring encodes in K-relations.
    """

    zero: bool = False
    one: bool = True

    def plus(self, a: bool, b: bool) -> bool:
        return a or b

    def times(self, a: bool, b: bool) -> bool:
        return a and b


@dataclass(frozen=True, slots=True)
class UnionSemiring(Generic[E]):
    """The set-union semiring ``(frozenset[E], union, union, empty, empty)``.

    An idempotent semiring where ``plus`` and ``times`` are both set union and
    both identities are the empty set. Used for where-provenance: each output
    column's annotation is the set of source ``ColumnRef``s that ultimately
    contributed to it.

    The zero-equals-one degeneracy is a feature here, not a bug: union is
    self-distributive and the semiring captures "merge contributors at every
    operator" cleanly. The semimodule extension (aggregate transfers) is what
    distinguishes per-property behaviour, not the multiplicative structure.
    """

    zero: frozenset[E] = frozenset()
    one: frozenset[E] = frozenset()

    def plus(self, a: frozenset[E], b: frozenset[E]) -> frozenset[E]:
        return a | b

    def times(self, a: frozenset[E], b: frozenset[E]) -> frozenset[E]:
        return a | b
