"""Demo nullability property: per-column tri-state {NON_NULL, NULLABLE, UNKNOWN}.

**Demo, not a production property.** It pins that a CTE-wrapped ``COALESCE``
propagates NON_NULL to the outer projection, and that a ``UNION ALL`` with one
nullable arm taints the combined output. Grounding is trivial here (every node
IMPLICIT) until the nullability discoverers land and consult ``not_null`` tests,
the declared ``nullable`` flag, and native ``NOT NULL`` constraints.

The lattice orders by precision: NON_NULL (the strongest guarantee) refines
NULLABLE refines UNKNOWN (the top, "no information"). ``meet`` keeps the stronger
guarantee, so resolving a ``not_null`` test against a permissive ``nullable: true``
flag yields NON_NULL. A structural property never contradicts, so the formal
lattice bottom (CONTRADICTION) is unreachable in propagation; it exists only to
make the lattice bounded.

The confluence combine is a semiring, not the lattice join. A ``UNION ALL`` is
nullable as soon as one arm is, so a branch proven NULLABLE taints the output even
against an UNKNOWN one. The lattice cannot express that: UNKNOWN is "no
information", which has to sit at the top so an opaque upstream never fails the
consistency check, and a join with the top is always the top, so no lattice with
UNKNOWN on top can give ``join(NULLABLE, UNKNOWN) == NULLABLE``. So nullability,
though idempotent, carries a semiring whose ``plus`` (confluence) and ``times``
(scalar inputs) take the more-null value, with NON_NULL as the identity. COALESCE
overrides with "non-null wins".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlglot import Expr
from sqlglot import expressions as exp

from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Opacity
from dblect.lineage.facts.property import AggregateRule, DepContext, Property, column_property
from dblect.lineage.graph import ColumnRef


class Nullability(StrEnum):
    CONTRADICTION = "contradiction"  # formal lattice bottom; unreachable in propagation
    NON_NULL = "non_null"
    NULLABLE = "nullable"
    UNKNOWN = "unknown"


# Precision rank: smaller is more precise. CONTRADICTION < NON_NULL < NULLABLE < UNKNOWN.
_RANK: dict[Nullability, int] = {
    Nullability.CONTRADICTION: 0,
    Nullability.NON_NULL: 1,
    Nullability.NULLABLE: 2,
    Nullability.UNKNOWN: 3,
}


def _meet(a: Nullability, b: Nullability) -> Nullability:
    return a if _RANK[a] <= _RANK[b] else b


def _join(a: Nullability, b: Nullability) -> Nullability:
    return a if _RANK[a] >= _RANK[b] else b


NULLABILITY_LATTICE: Lattice[Nullability] = Lattice(
    meet=_meet,
    join=_join,
    top=Nullability.UNKNOWN,
    bottom=Nullability.CONTRADICTION,
)


@dataclass(frozen=True, slots=True)
class NullabilitySemiring:
    """The null-taint combine: both ``plus`` (confluence) and ``times`` (scalar
    inputs) take the more-null value.

    NULLABLE beats UNKNOWN beats NON_NULL: a branch proven nullable taints the
    result regardless of what is unknown about the others, and UNKNOWN beats
    NON_NULL because we should not claim non-null without evidence. NON_NULL is
    the identity, so a non-null literal in an expression changes nothing.

    This is the join of the taint order NON_NULL < UNKNOWN < NULLABLE, which is
    not the precision order the lattice meet and join use (there UNKNOWN is the
    top). CONTRADICTION is the lattice bottom and never reaches the combine, so
    the laws are pinned over the three operational values; see
    ``test_nullability_semiring_laws``.
    """

    zero: Nullability = Nullability.NON_NULL
    one: Nullability = Nullability.NON_NULL

    def plus(self, a: Nullability, b: Nullability) -> Nullability:
        if a is Nullability.NULLABLE or b is Nullability.NULLABLE:
            return Nullability.NULLABLE
        if a is Nullability.UNKNOWN or b is Nullability.UNKNOWN:
            return Nullability.UNKNOWN
        return Nullability.NON_NULL

    def times(self, a: Nullability, b: Nullability) -> Nullability:
        return self.plus(a, b)


def _ground_unknown(_: ColumnRef) -> Annotation[Nullability]:
    """Trivial grounding: nothing is declared yet, so every node is IMPLICIT top."""
    return Annotation(Nullability.UNKNOWN, Opacity.IMPLICIT)


def _coalesce_rule(
    _expr: Expr, kids: tuple[Annotation[Nullability], ...], _ctx: DepContext
) -> Annotation[Nullability]:
    """``COALESCE`` is non-null as soon as one argument is, whatever the rest are."""
    provisional = any(k.provisional for k in kids)
    values = [k.value for k in kids]
    if not values:
        return Annotation(Nullability.UNKNOWN, Opacity.IMPLICIT, provisional=provisional)
    if any(v is Nullability.NON_NULL for v in values):
        return Annotation(Nullability.NON_NULL, provisional=provisional)
    if all(v is Nullability.NULLABLE for v in values):
        return Annotation(Nullability.NULLABLE, provisional=provisional)
    return Annotation(Nullability.UNKNOWN, Opacity.IMPLICIT, provisional=provisional)


def _is_not_null_rule(
    _expr: Expr, kids: tuple[Annotation[Nullability], ...], _ctx: DepContext
) -> Annotation[Nullability]:
    """``x IS NOT NULL`` is a boolean that is itself never null."""
    provisional = any(k.provisional for k in kids)
    return Annotation(Nullability.NON_NULL, provisional=provisional)


def _count_core(_expr: exp.AggFunc, child: Annotation[Nullability]) -> Annotation[Nullability]:
    """COUNT returns 0 for empty groups, never NULL."""
    return Annotation(Nullability.NON_NULL, provisional=child.provisional)


nullability: Property[Nullability, ColumnRef] = column_property(
    name="nullability",
    lattice=NULLABILITY_LATTICE,
    operators={
        exp.Coalesce: _coalesce_rule,
        exp.Is: _is_not_null_rule,
    },
    aggregates={
        exp.Count: AggregateRule(core=_count_core),
    },
    ground=_ground_unknown,
    semiring=NullabilitySemiring(),
)
