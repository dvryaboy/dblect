"""Demo aggregation-depth property: per-column count of stacked aggregates.

**Demo, not a production detector.** A ``SUM(t.x)`` in a CTE re-aggregated
by ``SUM(r.total)`` at the outer projection surfaces as depth 2; a
"double aggregation" check is ``depth > 1`` per model column.

Gaps before this is consumable as a real detector:

* Window functions (``SUM(x) OVER (...)``) are a different sqlglot class
  and aren't picked up.
* GROUP BY / HAVING context isn't surfaced; the intent is to flag the dbt
  pattern of pre-aggregating in a CTE and re-aggregating downstream, not
  syntactically illegal SQL-level nesting.
* DISTINCT / FILTER / ORDER BY inside aggregates aren't analysed.

The algebra is the max-semiring on non-negative ints: ``plus`` and
``times`` both take the max, so UNION arms and times-folded children
inherit the deepest path.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import expressions as exp

from dblect.lineage.graph import ColumnRef
from dblect.lineage.property import Property


@dataclass(frozen=True, slots=True)
class MaxSemiring:
    """Max-semiring on non-negative ints. Non-strict near-semiring."""

    zero: int = 0
    one: int = 0

    def plus(self, a: int, b: int) -> int:
        return max(a, b)

    def times(self, a: int, b: int) -> int:
        return max(a, b)


def _source_zero(_: ColumnRef) -> int:
    return 0


def _aggfunc_rule(expr: exp.AggFunc, child_k: int) -> int:
    return child_k + 1


aggregation_depth: Property[int] = Property(
    name="aggregation_depth",
    semiring=MaxSemiring(),
    source=_source_zero,
    operators={},
    aggregates={exp.AggFunc: _aggfunc_rule},
    unknown_value=0,
)
