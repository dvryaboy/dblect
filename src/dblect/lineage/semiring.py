"""The small algebraic interface every property gets to assume.

A property propagates values of some type ``K`` over the lineage graph. At
two points in the walk those values need to combine:

* ``times``: when SQL builds something from several inputs (an expression
  like ``a + b``, a JOIN).
* ``plus``: when several branches feed the same output (UNION ALL, multiple
  paths through a CTE).

Both are associative and commutative, both have identities (``one`` and
``zero``), and ``times`` distributes over ``plus``. That structure is a
*commutative semiring*, which the K-relations framework (Green, Karvounarakis,
Tannen "Provenance Semirings", PODS 2007) identifies as exactly what's needed
for any order-independent combine-rule. Strict semirings additionally satisfy
``0 x x = 0`` ("times an absent value annihilates"); the looser flavour used
by ``UnionSemiring`` doesn't.

Concrete implementations:

* ``BooleanSemiring``: strict; ``plus`` is ``or``, ``times`` is ``and``.
  Encodes plain set semantics over tuples and is the reference any property
  can be checked against.
* ``UnionSemiring``: non-strict; ``zero == one == empty set``, both
  operations are set union. Natural for where-provenance, where every
  operator unions the inputs' source-column sets.

Laws are checked in ``tests/lineage/test_semiring_laws.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

K = TypeVar("K")
E = TypeVar("E")


@runtime_checkable
class Semiring(Protocol[K]):
    """The minimal algebra a property's combine-rules satisfy.

    ``zero`` and ``one`` are declared as read-only properties so that frozen
    dataclass implementations (the natural way to build a concrete semiring)
    satisfy the protocol cleanly.
    """

    @property
    def zero(self) -> K: ...

    @property
    def one(self) -> K: ...

    def plus(self, a: K, b: K) -> K:
        """Combine values from branches that flow to the same output (UNION ALL,
        multiple paths through a CTE). Associative, commutative; ``zero`` is the
        identity."""
        ...

    def times(self, a: K, b: K) -> K:
        """Combine values from inputs to the same expression or JOIN.
        Associative, commutative; ``one`` is the identity. Strict semirings
        also satisfy ``zero x x = zero`` (annihilation)."""
        ...


@dataclass(frozen=True, slots=True)
class BooleanSemiring:
    """The Boolean semiring: values are ``True``/``False``, ``plus`` is ``or``,
    ``times`` is ``and``.

    This is the smallest strict semiring and the reference any property can
    be sanity-checked against. In K-relations terms it encodes plain set
    semantics: "is this tuple present in the output?"
    """

    zero: bool = False
    one: bool = True

    def plus(self, a: bool, b: bool) -> bool:
        return a or b

    def times(self, a: bool, b: bool) -> bool:
        return a and b


@dataclass(frozen=True, slots=True)
class UnionSemiring(Generic[E]):
    """The set-union semiring: values are frozen sets, both ``plus`` and
    ``times`` are union, both identities are the empty set.

    The natural shape for where-provenance: every operator and JOIN unions
    the sets of contributing source columns. The ``zero == one == empty
    set`` collapse is deliberate: it's what makes "merge contributors at
    every step" fall out of the algebra. The price is that this isn't a
    strict semiring (``0 x x = 0`` fails because ``empty | x == x``);
    properties that rely on absorption should use a strict semiring instead.
    Per-property behaviour for aggregates lives in the semimodule on top,
    not in the multiplicative structure here.
    """

    zero: frozenset[E] = frozenset()
    one: frozenset[E] = frozenset()

    def plus(self, a: frozenset[E], b: frozenset[E]) -> frozenset[E]:
        return a | b

    def times(self, a: frozenset[E], b: frozenset[E]) -> frozenset[E]:
        return a | b
