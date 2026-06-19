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


def _agg(sql_fn: str) -> exp.AggFunc:
    tree = sqlglot.parse_one(f"SELECT {sql_fn}(x) AS v FROM t GROUP BY k", dialect="duckdb")
    aggs = list(tree.find_all(exp.AggFunc))
    assert len(aggs) == 1, f"{sql_fn} did not parse to a single aggregate: {aggs}"
    return aggs[0]


def test_combining_aggregates_combine() -> None:
    # Synthesize a new value out of many: a total, a mean, a spread, a middle. These are
    # the ones a varying per-row companion corrupts, so they carry the obligation.
    for fn in ("SUM", "AVG", "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE", "MEDIAN", "MODE"):
        assert aggregate_behavior(_agg(fn)) is AggregateBehavior.COMBINE, fn


def test_selecting_aggregates_select() -> None:
    # Return one of the input values: the value is real, only its tag is uncertain.
    for fn in ("MIN", "MAX", "ANY_VALUE"):
        assert aggregate_behavior(_agg(fn)) is AggregateBehavior.SELECT, fn


def test_counting_aggregates_count() -> None:
    # Ignore the magnitude, yield a cardinality. Always safe whatever they count.
    for fn in ("COUNT", "COUNT_IF"):
        assert aggregate_behavior(_agg(fn)) is AggregateBehavior.COUNT, fn


def test_unclassified_aggregate_is_none() -> None:
    # The classification is an explicit allowlist; an aggregate with no entry is left
    # unclassified, which the lenient default reads as "no obligation" rather than
    # guessing. corr() reduces two columns and is deliberately not classified yet.
    tree = sqlglot.parse_one("SELECT corr(x, y) AS v FROM t GROUP BY k", dialect="duckdb")
    [corr] = list(tree.find_all(exp.AggFunc))
    assert aggregate_behavior(corr) is None


def test_lookup_walks_the_mro() -> None:
    # A subclass of a classified aggregate inherits its behavior, matching the
    # propagator's own MRO-based aggregate dispatch.
    class _MySum(exp.Sum):
        pass

    assert aggregate_behavior(_MySum(this=exp.column("x"))) is AggregateBehavior.COMBINE
