"""The reduction-behavior classification of aggregate functions.

These pin the contract the coherence guard and the not-well-typed finding both read:
which aggregates combine values (carry the currency obligation), which select a real
value (widen, no obligation at the operation), and which count (tag-free). The point of
the classification is that this is one table, not a rule scattered across the finding
and the guard.
"""

from __future__ import annotations

import sqlglot
import sqlglot.expressions as exp

from dblect.sql import AggregateBehavior, aggregate_behavior


def _agg(call: str) -> exp.AggFunc:
    tree = sqlglot.parse_one(f"SELECT {call} AS v FROM t GROUP BY k", dialect="duckdb")
    aggs = list(tree.find_all(exp.AggFunc))
    assert len(aggs) == 1, f"{call} did not parse to a single aggregate: {aggs}"
    return aggs[0]


def test_combining_aggregates_combine() -> None:
    # Synthesize a new value out of many: a total, a mean, a spread, a moment, a middle,
    # a quantile. These are the ones a varying per-row companion corrupts.
    calls = (
        "sum(x)",
        "avg(x)",
        "stddev(x)",
        "stddev_pop(x)",
        "variance(x)",
        "kurtosis(x)",
        "skewness(x)",
        "median(x)",
        "mode(x)",
        "quantile(x, 0.5)",
        "percentile_cont(x, 0.5)",
        "approx_quantile(x, 0.5)",
    )
    for call in calls:
        assert aggregate_behavior(_agg(call)) is AggregateBehavior.COMBINE, call


def test_selecting_aggregates_select() -> None:
    # Return one of the input values: the value is real, only its tag is uncertain.
    # bigquery max_by/min_by parse to arg_max/arg_min.
    for call in ("min(x)", "max(x)", "any_value(x)", "arg_max(x, y)", "arg_min(x, y)"):
        assert aggregate_behavior(_agg(call)) is AggregateBehavior.SELECT, call


def test_counting_aggregates_count() -> None:
    # Ignore the magnitude, yield a cardinality. Always safe whatever they count.
    for call in ("count(x)", "count_if(x > 0)"):
        assert aggregate_behavior(_agg(call)) is AggregateBehavior.COUNT, call


def test_no_magnitude_obligation_families_are_unclassified() -> None:
    # Collection, boolean, bitwise, and two-argument statistical aggregates carry no
    # magnitude obligation, so they are deliberately left unclassified; the lenient
    # default reads that as "nothing to discharge" rather than guessing.
    for call in ("array_agg(x)", "string_agg(x, ',')", "bool_and(x)", "bit_xor(x)", "corr(x, y)"):
        assert aggregate_behavior(_agg(call)) is None, call


def test_lookup_walks_the_mro() -> None:
    # A subclass of a classified aggregate inherits its behavior, matching the
    # propagator's own MRO-based aggregate dispatch.
    class _MySum(exp.Sum):
        pass

    assert aggregate_behavior(_MySum(this=exp.column("x"))) is AggregateBehavior.COMBINE
