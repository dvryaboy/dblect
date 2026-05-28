"""Demo aggregation-depth property: per-column count of stacked aggregates.

**This is a demo, not a production detector.** It exists to exercise the
substrate's structural reshape from #25: ``SUM(t.x)`` through a CTE and
then ``SUM(r.total)`` at the outer projection should surface as depth 2,
because the substrate now exposes the CTE projection as a first-class
graph entry rather than collapsing both ``SUM``s into a single
``Column → leaf`` stamp. The detector that wants to flag double
aggregation (a common dbt foot-gun) can then check
``depth > 1`` per column.

What's missing for production use is tracked in #26; the rough list:

* Window functions are ignored. ``SUM(x) OVER (PARTITION BY y)`` is an
  aggregate semantically but a different sqlglot expression class; the
  rule here doesn't fire on it.
* GROUP BY / HAVING context isn't surfaced. Nested aggregates are usually
  illegal at the SQL level; what we mean by "depth > 1" is the dbt
  pattern of pre-aggregating in a CTE and re-aggregating downstream,
  which is legal but often unintended.
* DISTINCT, FILTER, and ORDER BY inside aggregates aren't analysed.

The algebra is the max-semiring on non-negative ints: ``plus`` and
``times`` both ``max``. This makes UNION arms and times-folded children
inherit the *deepest* path's depth, which is the right answer for
"does any branch already aggregate?"
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import expressions as exp

from dblect.lineage.graph import ColumnRef
from dblect.lineage.property import Property


@dataclass(frozen=True, slots=True)
class MaxSemiring:
    """Max-semiring on non-negative ints.

    ``plus`` and ``times`` both take the larger of two depths. Identities
    ``zero == one == 0`` keep both operations honest. The semiring is
    non-strict (``times(0, x) == x``, not ``0``), the same near-semiring
    shape as ``UnionSemiring``.
    """

    zero: int = 0
    one: int = 0

    def plus(self, a: int, b: int) -> int:
        return max(a, b)

    def times(self, a: int, b: int) -> int:
        return max(a, b)


def _source_zero(_: ColumnRef) -> int:
    """Raw source columns have aggregation depth 0."""
    return 0


def _aggfunc_rule(expr: exp.AggFunc, child_k: int) -> int:
    """Each aggregate adds one to its input's depth.

    ``SUM(x)`` over a depth-0 input is depth 1. ``SUM(SUM(x))`` (via a
    CTE wrap) is depth 2: the inner ``SUM`` annotated its CTE column as
    1, the outer reference inherits that 1 via the Column stamp, and the
    outer ``SUM`` adds one more.
    """
    return child_k + 1


aggregation_depth: Property[int] = Property(
    name="aggregation_depth",
    semiring=MaxSemiring(),
    source=_source_zero,
    operators={},
    aggregates={exp.AggFunc: _aggfunc_rule},
    unknown_value=0,
)
