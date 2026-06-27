"""Array-literal non-emptiness recognition.

The detector and the ``array_nonemptiness`` property both read ``array_literal_nonempty`` to
decide whether an array constructor is provably non-empty. The soundness line is between a
bracket list (every element guaranteed present) and a set-returning subquery element (which
can be empty), and it is subtle because BigQuery's array-subquery form and a bracket of
parenthesised scalar subqueries parse to the same ``exp.Array`` shape, separated only by
whether the element subquery reads a ``FROM``.
"""

from __future__ import annotations

import pytest
import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import parse_sql
from dblect.sql.vocab import array_literal_nonempty


def _array(frag: str) -> Expr:
    """The expression a single projection parses to, unwrapping its alias."""
    select = parse_sql(f"SELECT {frag} AS a", dialect="bigquery")
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
