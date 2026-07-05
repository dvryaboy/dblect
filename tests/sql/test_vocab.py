"""Intrinsic array non-emptiness recognition.

The inner-flatten detector and the ``array_nonemptiness`` property both read these vocab
predicates to decide whether an array constructor is provably non-empty from the node alone.
For ``array_literal_nonempty`` the soundness line is between a bracket list (every element
guaranteed present) and a set-returning subquery element (which can be empty), subtle because
BigQuery's array-subquery form and a bracket of parenthesised scalar subqueries parse to the
same ``exp.Array`` shape, separated only by whether the element subquery reads a ``FROM``. For
``generator_provably_nonempty`` the line is at literal, order-comparable bounds with a readable
step sign: numeric series and literal date spines are proved; column bounds and timestamp
generators (unsound to compare across timezone offsets) are left firing.
"""

from __future__ import annotations

import pytest
import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import parse_sql
from dblect.sql.vocab import array_literal_nonempty, generator_provably_nonempty


def _array(frag: str, dialect: str = "bigquery") -> Expr:
    """The expression a single projection parses to, unwrapping its alias."""
    select = parse_sql(f"SELECT {frag} AS a", dialect=dialect)
    assert isinstance(select, exp.Select)
    projected = select.expressions[0]
    return projected.this if isinstance(projected, exp.Alias) else projected


@pytest.mark.parametrize(
    ("frag", "nonempty"),
    [
        ("[1, 2, 3]", True),
        ("ARRAY[1, 2]", True),
        ("ARRAY[STRUCT('clicks' AS k, x AS v)]", True),
        # The wide-to-long pivot idiom: parenthesised scalar subqueries with no FROM, each
        # exactly one row.
        ("[(SELECT AS STRUCT 1 AS a), (SELECT AS STRUCT 2 AS a)]", True),
        ("ARRAY((SELECT AS STRUCT 1 AS a))", True),
        # Empty constructor: no guarantee.
        ("ARRAY[]", False),
        # Array-subquery forms are set-returning and can be empty, whether bare or
        # parenthesised, and a filter can empty them out.
        ("ARRAY(SELECT v FROM u)", False),
        ("ARRAY((SELECT AS STRUCT name FROM UNNEST(m) WHERE name IN ('x')))", False),
    ],
)
def test_array_literal_nonempty(frag: str, nonempty: bool) -> None:
    assert array_literal_nonempty(_array(frag)) is nonempty


def test_non_array_expression_is_not_a_literal_array() -> None:
    assert array_literal_nonempty(_array("x + 1")) is False


@pytest.mark.parametrize(
    ("frag", "nonempty"),
    [
        # Literal ascending range under the default (+1) step: non-empty.
        ("GENERATE_SERIES(0, 23)", True),
        ("GENERATE_ARRAY(0, 23)", True),
        # A single-point inclusive range is one element, still non-empty.
        ("GENERATE_ARRAY(7, 7)", True),
        # Descending literal bounds under the default (+1) step yield an empty range.
        ("GENERATE_ARRAY(5, 1)", False),
        # An explicit negative step running downward is non-empty; running upward is empty.
        ("GENERATE_SERIES(10, 0, -2)", True),
        ("GENERATE_SERIES(0, 10, -2)", False),
        # A negative-literal range is decidable like any other.
        ("GENERATE_SERIES(-5, -1)", True),
        # Fractional literal bounds and step are still literals, so still decidable.
        ("GENERATE_ARRAY(1.0, 2.0, 0.5)", True),
        # A zero step has no well-defined range, so it is not proved non-empty.
        ("GENERATE_ARRAY(0, 10, 0)", False),
        # Non-literal bounds leave the range possibly empty: a count of 0, an empty input
        # array. Not provable from the call, so it stays firing.
        ("GENERATE_SERIES(1, CAST(n AS INT64))", False),
        ("GENERATE_SERIES(0, ARRAY_LENGTH(m) - 1)", False),
        # A date spine over literal bounds is the same intrinsic proof, mapped through the
        # calendar: an ascending range under the default (+1 day) step is non-empty, whether
        # the bounds are DATE-typed or bare date strings the generator coerces.
        ("GENERATE_DATE_ARRAY(DATE '2020-01-01', DATE '2020-12-31')", True),
        ("GENERATE_DATE_ARRAY('2020-01-01', '2020-12-31')", True),
        ("GENERATE_DATE_ARRAY(DATE '2020-01-01', DATE '2020-01-01')", True),
        # The interval's magnitude never changes non-emptiness (the start is always present);
        # only its sign is read, so a month step over an ascending range is still non-empty.
        ("GENERATE_DATE_ARRAY(DATE '2020-01-01', DATE '2020-12-31', INTERVAL 1 MONTH)", True),
        # Descending bounds under the default (+1) step are empty; a negative step reverses it.
        ("GENERATE_DATE_ARRAY(DATE '2020-12-31', DATE '2020-01-01')", False),
        ("GENERATE_DATE_ARRAY(DATE '2020-12-31', DATE '2020-01-01', INTERVAL -1 DAY)", True),
        ("GENERATE_DATE_ARRAY(DATE '2020-01-01', DATE '2020-12-31', INTERVAL -1 DAY)", False),
        # A non-literal step magnitude is unreadable, so the sign is unknown and it stays firing.
        ("GENERATE_DATE_ARRAY(DATE '2020-01-01', DATE '2020-12-31', INTERVAL n DAY)", False),
        # Column bounds can invert or be empty; not provable from the call.
        ("GENERATE_DATE_ARRAY(a, b)", False),
        # A literal that is not a canonical date does not parse to a calendar point, so it is
        # left unproven rather than guessed at.
        ("GENERATE_DATE_ARRAY('2020-13-01', '2020-14-01')", False),
        # Timestamp generators are deferred: a raw literal compare is unsound across timezone
        # offsets, so they stay firing whether the bounds are literal or column.
        (
            "GENERATE_TIMESTAMP_ARRAY(TIMESTAMP '2020-01-01', TIMESTAMP '2020-01-02', INTERVAL 1 HOUR)",
            False,
        ),
        ("GENERATE_TIMESTAMP_ARRAY(a, b, INTERVAL 1 HOUR)", False),
    ],
)
def test_generator_provably_nonempty(frag: str, nonempty: bool) -> None:
    assert generator_provably_nonempty(_array(frag)) is nonempty


@pytest.mark.parametrize(
    ("frag", "nonempty"),
    [
        # A Postgres/Redshift date spine spells the same idiom as GENERATE_DATE_ARRAY, through
        # generate_series over date-cast bounds with an explicit interval step. The comparable
        # domain is read off the bounds, not the function name, so the calendar proof reaches
        # this form too. Ascending bounds under a positive step are non-empty.
        ("generate_series('2020-01-01'::date, '2020-01-31'::date, interval '1 day')", True),
        # Descending bounds under a negative step run downward: still non-empty.
        ("generate_series('2020-01-31'::date, '2020-01-01'::date, interval '-1 day')", True),
        # Ascending bounds under a negative step (or descending under a positive one) is empty.
        ("generate_series('2020-01-01'::date, '2020-01-31'::date, interval '-1 day')", False),
        ("generate_series('2020-01-31'::date, '2020-01-01'::date, interval '1 day')", False),
        # The raw-string cast spelling of the step ('1 day'::interval) reads the same sign.
        ("generate_series('2020-01-01'::date, '2020-01-31'::date, '1 day'::interval)", True),
        ("generate_series('2020-01-31'::date, '2020-01-01'::date, '-1 day'::interval)", True),
        # A compound interval ('1 mon -1 day') has an ambiguous net direction, so its sign is
        # not read off the leading component and it stays unproven.
        (
            "generate_series('2020-01-01'::date, '2020-01-31'::date, '1 mon -1 day'::interval)",
            False,
        ),
        # Column date bounds can invert or be empty: not provable from the call.
        ("generate_series(a::date, b::date, interval '1 day')", False),
    ],
)
def test_generator_provably_nonempty_postgres_date_series(frag: str, nonempty: bool) -> None:
    assert generator_provably_nonempty(_array(frag, dialect="postgres")) is nonempty


def test_non_generator_expression_is_not_a_nonempty_generator() -> None:
    assert generator_provably_nonempty(_array("x + 1")) is False
