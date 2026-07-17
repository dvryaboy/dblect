"""The reduction-behavior classification of aggregate functions.

These pin the contract the coherence guard and the not-well-typed finding both read:
which aggregates combine values (carry the currency obligation), which select a real
value (widen, no obligation at the operation), and which count (tag-free). The point of
the classification is that this is one table, not a rule scattered across the finding
and the guard.
"""

from __future__ import annotations

import pytest
import sqlglot
import sqlglot.expressions as exp
import sqlglot.parser

from dblect.sql import (
    AggregateBehavior,
    aggregate_behavior,
    duplicate_sensitive,
    strips_duplicates,
)

# sqlglot's compiled build (sqlglotc) forbids instantiating an interpreted subclass of a
# compiled class, so the one test that defines a throwaway aggregate subclass is skipped
# there. The MRO-walk logic it exercises is pure-Python dblect code, identical either way.
_COMPILED_SQLGLOT = sqlglot.parser.__file__.endswith((".so", ".pyd"))


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


@pytest.mark.skipif(
    _COMPILED_SQLGLOT,
    reason="defines an interpreted subclass of a compiled sqlglot class; the MRO-walk "
    "logic under test is build-independent and covered on pure sqlglot",
)
def test_lookup_walks_the_mro() -> None:
    # A subclass of a classified aggregate inherits its behavior, matching the
    # propagator's own MRO-based aggregate dispatch.
    class _MySum(exp.Sum):
        pass

    assert aggregate_behavior(_MySum(this=exp.column("x"))) is AggregateBehavior.COMBINE


# --- Multiplicity axis: idempotency and duplicate-sensitivity (hazard-algebra) ---


def test_combining_and_counting_aggregates_are_duplicate_sensitive() -> None:
    # A fan-out that duplicates rows distorts these: sum doubles, count over-counts,
    # array_agg gathers the dupes, bit_xor cancels them.
    for call in ("sum(x)", "avg(x)", "count(x)", "count(*)", "array_agg(x)", "bit_xor(x)"):
        assert duplicate_sensitive(_agg(call)), call


def test_duplicate_safe_aggregates_are_not_duplicate_sensitive() -> None:
    # A duplicated row leaves the result unchanged (an idempotent combine, or a stable
    # selection), so a fan-out into these is harmless.
    for call in ("max(x)", "min(x)", "any_value(x)", "bool_and(x)", "bool_or(x)", "bit_and(x)"):
        assert not duplicate_sensitive(_agg(call)), call


def test_distinct_strips_duplicates_so_not_sensitive() -> None:
    # count(distinct x) / sum(distinct x) deduplicate before folding, so a fan-out that
    # only duplicates rows cannot change the result.
    for call in ("count(distinct x)", "sum(distinct x)"):
        assert strips_duplicates(_agg(call)), call
        assert not duplicate_sensitive(_agg(call)), call
    assert not strips_duplicates(_agg("sum(x)"))


def test_unclassified_anonymous_aggregate_defaults_to_sensitive() -> None:
    # An unrecognized UDF aggregate keeps a fan-out firing (the firewall default),
    # unless the adapter names it duplicate-safe.
    tree = sqlglot.parse_one("SELECT geometric_mean(x) AS v FROM t GROUP BY k", dialect="duckdb")
    udf = tree.find(exp.Anonymous)
    assert udf is not None
    assert duplicate_sensitive(udf)
    assert not duplicate_sensitive(udf, safe_builtins=frozenset({"geometric_mean"}))
