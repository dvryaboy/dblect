"""``JoinRowEffect``: per-join, per-``JoinSide`` decision of which aliases a join
NULL-pads and which aliases lose their unmatched rows outright.

One row per ``JoinSide`` sqlglot can produce, each a two-way ``a <SIDE> JOIN b``
so ``accumulated_left = {"a"}`` and ``right = "b"``. ``outer_join_optional_aliases``
and ``joins_with_outer_dropped_aliases`` are now projections of this table (see
their docstrings in ``_sqlglot.py``); their pre-existing callers (the nullability
property, ``detect_join_on_nullable_key``) pin those projections' contracts.
"""

from __future__ import annotations

from typing import cast

import pytest
import sqlglot.expressions as exp

from dblect.sql._sqlglot import JoinSide, join_row_effects
from dblect.sql.parse import parse_sql


def _select(sql: str) -> exp.Select:
    return cast("exp.Select", parse_sql(sql, dialect="duckdb"))


# id, sql, side, optional, dropped_unmatched
_TWO_WAY: list[tuple[str, str, JoinSide, frozenset[str], frozenset[str]]] = [
    (
        "inner",
        "select * from a inner join b on a.x = b.x",
        JoinSide.INNER,
        frozenset(),
        frozenset({"a", "b"}),  # neither side's unmatched row survives an inner join
    ),
    (
        "left",
        "select * from a left join b on a.x = b.x",
        JoinSide.LEFT,
        frozenset({"b"}),
        frozenset({"b"}),
    ),
    (
        "right",
        "select * from a right join b on a.x = b.x",
        JoinSide.RIGHT,
        frozenset({"a"}),
        frozenset({"a"}),
    ),
    (
        "full",
        "select * from a full join b on a.x = b.x",
        JoinSide.FULL,
        frozenset({"a", "b"}),
        frozenset(),  # both sides survive NULL-padded; nothing is dropped
    ),
    (
        "cross",
        "select * from a cross join b",
        JoinSide.CROSS,
        frozenset(),
        frozenset(),  # no match predicate, so nothing is "unmatched"
    ),
    (
        "semi",
        "select * from a semi join b on a.x = b.x",
        JoinSide.SEMI,
        frozenset(),
        frozenset({"a"}),  # a probe row (accumulated left) without a match is dropped
    ),
    (
        "anti",
        "select * from a anti join b on a.x = b.x",
        JoinSide.ANTI,
        frozenset(),
        frozenset(),  # unmatched rows are exactly what an anti-join keeps
    ),
]


@pytest.mark.parametrize(
    ("sql", "side", "optional", "dropped"),
    [(sql, side, optional, dropped) for _id, sql, side, optional, dropped in _TWO_WAY],
    ids=[id_ for id_, *_ in _TWO_WAY],
)
def test_join_row_effect_per_side(
    sql: str, side: JoinSide, optional: frozenset[str], dropped: frozenset[str]
) -> None:
    [effect] = join_row_effects(_select(sql))
    assert effect.side is side
    assert effect.optional == optional
    assert effect.dropped_unmatched == dropped
