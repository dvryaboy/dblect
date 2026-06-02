"""Demo aggregation-depth property: per-column count of stacked aggregates.

**Demo, not a production detector.** A ``SUM(t.x)`` in a CTE re-aggregated by
``SUM(r.total)`` at the outer projection surfaces as depth 2; a "double
aggregation" check is ``depth > 1`` per model column. The gaps before this is a
real detector (window functions, GROUP BY/HAVING context, DISTINCT/FILTER inside
aggregates) are unchanged by this migration.

The engine is the max-semiring on non-negative ints: ``plus`` and ``times`` both
take the max, so UNION arms and times-folded children inherit the deepest path.
Like where-provenance, aggregation depth has nothing to declare, so its grounding
is empty and its lattice is nominal: the only field the propagator reads is
``top`` (zero, "no aggregation"), used to classify a zero-depth result as
carrying no information. Its combine laws are the max-semiring laws, checked in
``test_semiring_laws``.
"""

from __future__ import annotations

from sqlglot import expressions as exp

from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Opacity
from dblect.lineage.facts.property import AggregateRule, Property, column_property
from dblect.lineage.graph import ColumnRef
from dblect.lineage.semiring import Semiring


class MaxSemiring:
    """Max-semiring on non-negative ints. Non-strict near-semiring; ``plus`` and
    ``times`` both take the max, so the deepest path wins at confluence and cross."""

    zero: int = 0
    one: int = 0

    def plus(self, a: int, b: int) -> int:
        return max(a, b)

    def times(self, a: int, b: int) -> int:
        return max(a, b)


# Nominal lattice: aggregation depth is driven entirely by its max semiring. Only
# ``top`` (zero) is read at runtime, classifying a zero-depth result as carrying
# no information; meet/join/bottom are inert because nothing declares a depth.
_DEPTH_LATTICE: Lattice[int] = Lattice(
    meet=min,
    join=max,
    top=0,
    bottom=0,
)


def _ground_zero(_: ColumnRef) -> Annotation[int]:
    return Annotation(0, Opacity.IMPLICIT)


def _aggfunc_core(_expr: exp.AggFunc, child: Annotation[int]) -> Annotation[int]:
    return Annotation(child.value + 1, provisional=child.provisional)


_MAX_SEMIRING: Semiring[int] = MaxSemiring()

aggregation_depth: Property[int, ColumnRef] = column_property(
    name="aggregation_depth",
    lattice=_DEPTH_LATTICE,
    operators={},
    aggregates={exp.AggFunc: AggregateRule(core=_aggfunc_core)},
    ground=_ground_zero,
    semiring=_MAX_SEMIRING,
)
