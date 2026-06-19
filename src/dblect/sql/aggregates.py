"""Reduction-behavior classification of aggregate functions.

An aggregate over a tagged magnitude (a ``Money`` amount) does one of three things to
the values it folds, and which one decides the currency-coherence obligation
(``docs/design/domain-type-algebra.md``):

* **COMBINE** synthesizes a new value out of many (``sum``, ``avg``, a spread, a
  middle). A per-row companion that varies within the group corrupts the result, so a
  combining reduction carries the coherence obligation.
* **SELECT** returns one of the input values (``min``, ``max``, ``arg_min``). The value
  it returns is real, so the operation does not fail; only its tag is uncertain, because
  the comparison that chose it was tag-blind. The result widens to top, caught wherever
  a definite tag is later required.
* **COUNT** ignores the magnitude and yields a tag-free cardinality (``count``), always
  safe whatever it counts.

The classification keys on the sqlglot expression *type*, the same key the propagator's
aggregate dispatch already uses, and is the single source of truth for both arming the
coherence guard and the not-well-typed finding. It is an explicit allowlist: an
aggregate with no entry is left unclassified, which the lenient default reads as "no
obligation" rather than guessing. A dialect adapter extends the portable base with its
own aggregates the same way :data:`PORTABLE_NON_DETERMINISTIC_BUILTINS` is extended,
carrying the merged map on its :class:`~dblect.adapters.AdapterProfile`.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum, auto

import sqlglot.expressions as exp

__all__ = [
    "PORTABLE_AGGREGATE_BEHAVIOR",
    "AggregateBehavior",
    "aggregate_behavior",
]


class AggregateBehavior(Enum):
    """How an aggregate treats the values it folds: see the module docstring."""

    COMBINE = auto()
    SELECT = auto()
    COUNT = auto()


# The portable base: aggregates whose behavior is the same on every warehouse. A dialect
# adapter merges its own entries onto this (``PORTABLE_AGGREGATE_BEHAVIOR | {...}``).
#
# Deliberately left unclassified (no magnitude obligation, so the lenient default is
# correct and an explicit entry would only add noise):
#   * collection aggregates (``array_agg``/``list``, ``string_agg``, ``histogram``) gather
#     values into a container rather than reducing a magnitude;
#   * boolean (``bool_and``/``logical_and``, ``bool_or``) and bitwise (``bit_and/or/xor``)
#     folds operate on non-magnitude domains;
#   * two-argument statistical aggregates (``corr``, ``covar_*``, ``regr_*``) produce a
#     scale-invariant or product-typed result whose coherence is a separate question.
# Dialect aggregates sqlglot parses as anonymous (duckdb ``product``, ``geometric_mean``,
# ``favg``, ``fsum``, ``mad``, ``entropy``, ...) cannot be type-keyed at all; classifying
# them by name is tracked in #119.
PORTABLE_AGGREGATE_BEHAVIOR: Mapping[type[exp.AggFunc], AggregateBehavior] = {
    # COMBINE: synthesize a new value out of many (a total, mean, spread, moment, middle,
    # or quantile). Mixing units across the reduced rows corrupts the result.
    exp.Sum: AggregateBehavior.COMBINE,
    exp.Avg: AggregateBehavior.COMBINE,
    exp.Stddev: AggregateBehavior.COMBINE,
    exp.StddevPop: AggregateBehavior.COMBINE,
    exp.StddevSamp: AggregateBehavior.COMBINE,
    exp.Variance: AggregateBehavior.COMBINE,
    exp.VariancePop: AggregateBehavior.COMBINE,
    exp.Kurtosis: AggregateBehavior.COMBINE,
    exp.Skewness: AggregateBehavior.COMBINE,
    exp.Median: AggregateBehavior.COMBINE,
    exp.Mode: AggregateBehavior.COMBINE,
    exp.Quantile: AggregateBehavior.COMBINE,
    exp.ApproxQuantile: AggregateBehavior.COMBINE,
    exp.PercentileCont: AggregateBehavior.COMBINE,
    exp.PercentileDisc: AggregateBehavior.COMBINE,
    # SELECT: return one of the input values (bigquery ``max_by``/``min_by`` parse to
    # ``ArgMax``/``ArgMin``). The value is real; only its tag is uncertain.
    exp.Min: AggregateBehavior.SELECT,
    exp.Max: AggregateBehavior.SELECT,
    exp.ArgMin: AggregateBehavior.SELECT,
    exp.ArgMax: AggregateBehavior.SELECT,
    exp.AnyValue: AggregateBehavior.SELECT,
    exp.First: AggregateBehavior.SELECT,
    exp.Last: AggregateBehavior.SELECT,
    # COUNT: ignore the magnitude, yield a cardinality.
    exp.Count: AggregateBehavior.COUNT,
    exp.CountIf: AggregateBehavior.COUNT,
    exp.ApproxDistinct: AggregateBehavior.COUNT,
}


def aggregate_behavior(
    agg: exp.AggFunc,
    classification: Mapping[type[exp.AggFunc], AggregateBehavior] = PORTABLE_AGGREGATE_BEHAVIOR,
) -> AggregateBehavior | None:
    """The behavior class of ``agg`` under ``classification``, or ``None`` if unclassified.

    Lookup walks the type's MRO so a rule on a base aggregate catches its subclasses,
    matching the propagator's own aggregate dispatch. ``classification`` defaults to the
    portable base; a run passes the resolved adapter's merged map to honor dialect
    extensions."""
    for cls in type(agg).__mro__:
        if not issubclass(cls, exp.AggFunc):
            break  # left the AggFunc hierarchy; MRO bases past here are never aggregates
        behavior = classification.get(cls)
        if behavior is not None:
            return behavior
    return None
