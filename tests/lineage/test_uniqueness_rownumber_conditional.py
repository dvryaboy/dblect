"""A projected ``ROW_NUMBER() OVER (PARTITION BY p ...) AS rn`` is a conditional key.

``p`` is unique over exactly the rows where ``rn = 1``, so the window's own model
carries ``p`` as a key under the predicate ``rn <= 1`` and a consumer that filters
``rn`` down to the top rank activates it, whether the filter sits in a WHERE, in an
INNER or LEFT join's ON, in a CTE of the same model, or in another model. These pin
that at the propagation boundary: the closed predicate and join-site grids decide each
case, and each non-activating case is a counterexample to the nearest activating one.
Every source declares no key, so the rule under test is the only thing that can ground one.
"""

from __future__ import annotations

import pytest

from dblect.lineage.properties.uniqueness import Key
from tests.lineage._rownumber_keys import COLS as _COLS
from tests.lineage._rownumber_keys import model as _model
from tests.lineage._rownumber_keys import promoted_keys as _promoted
from tests.lineage._rownumber_keys import window_sql as _window


def _consumer_keys(where: str, *, window: str | None = None) -> frozenset[Key]:
    """Keys of a consumer model that filters the window model's output by ``where``,
    projecting every column but ``rn``."""
    return _promoted(
        "b",
        _model("a", window or _window()),
        _model("b", f"SELECT {_COLS} FROM a WHERE {where}", "a"),
    )


# --- the predicate grid (WHERE in another model) ----------------------------------

# Predicates implying ``rn <= 1``: every one keeps at most the top row per partition.
_ACTIVATING = (
    "rn = 1",
    "1 = rn",
    "rn <= 1",
    "1 >= rn",
    "rn < 2",
    "rn IN (1)",
    "a.rn = 1",
    "rn = 1 AND c0 > 5",
    "(rn = 1)",
)

# Predicates that keep a row beyond the top one in some partition, or that no
# conjunction of comparisons proves to keep only the top: each leaves duplicates.
_NON_ACTIVATING = (
    "rn = 2",
    "rn <= 2",
    "rn > 1",
    "rn >= 1",
    "rn <> 1",
    "rn IN (1, 2)",
    "rn IS NULL",
    "rn = 1 OR c0 > 5",
    "c0 = 1",
    "c1 = 1",
    "TRUE",
)


@pytest.mark.parametrize("where", _ACTIVATING)
def test_a_filter_keeping_only_the_top_rank_activates_the_partition_key(where: str) -> None:
    assert _consumer_keys(where) == {frozenset({"c1"})}


@pytest.mark.parametrize("where", _NON_ACTIVATING)
def test_a_filter_that_can_keep_a_lower_rank_activates_nothing(where: str) -> None:
    assert _consumer_keys(where) == frozenset()


def test_no_filter_activates_nothing() -> None:
    assert (
        _promoted("b", _model("a", _window()), _model("b", f"SELECT {_COLS} FROM a", "a"))
        == frozenset()
    )


@pytest.mark.parametrize("fn", ["RANK", "DENSE_RANK"])
def test_a_rank_that_ties_mints_no_key(fn: str) -> None:
    """RANK and DENSE_RANK give several rows the rank 1 on a tie, so ``rn = 1`` keeps
    duplicates of the partition."""
    assert _consumer_keys("rn = 1", window=_window(fn)) == frozenset()


def test_a_partitionless_window_mints_no_key() -> None:
    sql = f"SELECT {_COLS}, ROW_NUMBER() OVER (ORDER BY c0) AS rn FROM events"
    assert _consumer_keys("rn = 1", window=sql) == frozenset()


def test_an_expression_partition_mints_no_key() -> None:
    sql = f"SELECT {_COLS}, ROW_NUMBER() OVER (PARTITION BY c1 + 1 ORDER BY c0) AS rn FROM events"
    assert _consumer_keys("rn = 1", window=sql) == frozenset()


def test_a_partition_column_the_window_model_does_not_project_mints_no_key() -> None:
    sql = "SELECT c0, c2, ROW_NUMBER() OVER (PARTITION BY c1 ORDER BY c0) AS rn FROM events"
    assert _consumer_keys("rn = 1", window=sql) == frozenset()


def test_the_key_is_the_whole_partition() -> None:
    sql = f"SELECT {_COLS}, ROW_NUMBER() OVER (PARTITION BY c1, c2 ORDER BY c0) AS rn FROM events"
    assert _consumer_keys("rn = 1", window=sql) == {frozenset({"c1", "c2"})}


def test_the_key_follows_a_renamed_partition_column() -> None:
    sql = (
        "SELECT c0, c1 AS grp, c2, "
        "ROW_NUMBER() OVER (PARTITION BY c1 ORDER BY c0) AS rn FROM events"
    )
    keys = _promoted(
        "b",
        _model("a", sql),
        _model("b", "SELECT c0, grp, c2 FROM a WHERE rn = 1", "a"),
    )
    assert keys == {frozenset({"grp"})}


def test_a_window_model_without_the_filter_keeps_the_key_conditional() -> None:
    """The window model itself has every row, so the partition is not its key."""
    assert _promoted("a", _model("a", _window())) == frozenset()


def test_the_conditional_survives_a_pass_through_model() -> None:
    keys = _promoted(
        "c",
        _model("a", _window()),
        _model("b", "SELECT * FROM a", "a"),
        _model("c", f"SELECT {_COLS} FROM b WHERE rn = 1", "b"),
    )
    assert keys == {frozenset({"c1"})}


def test_the_conditional_dies_when_the_pass_through_drops_the_rank() -> None:
    keys = _promoted(
        "c",
        _model("a", _window()),
        _model("b", f"SELECT {_COLS} FROM a", "a"),
        _model("c", f"SELECT {_COLS} FROM b WHERE rn = 1", "b"),
    )
    assert keys == frozenset()


# --- join sites (one model, the window in a CTE) ------------------------------------

_CTES = f"WITH f AS ({_window()}), d AS (SELECT DISTINCT c0 FROM events) SELECT d.c0, f.c2 FROM "
_D_KEY: frozenset[Key] = frozenset({frozenset({"c0"})})
_NO_KEYS: frozenset[Key] = frozenset()


def _joined(tail: str) -> frozenset[Key]:
    return _promoted("m", _model("m", _CTES + tail))


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        # An ON conjunct on the joined alias filters that side before it joins, so it
        # holds for every INNER or LEFT match.
        ("d JOIN f ON d.c0 = f.c1 AND f.rn = 1", _D_KEY),
        ("d LEFT JOIN f ON d.c0 = f.c1 AND f.rn = 1", _D_KEY),
        ("d JOIN f ON d.c0 = f.c1 AND f.rn <= 1", _D_KEY),
        ("d JOIN f ON d.c0 = f.c1 AND 1 = f.rn", _D_KEY),
        # A WHERE conjunct filters the joined rows, padded ones included.
        ("d JOIN f ON d.c0 = f.c1 WHERE f.rn = 1", _D_KEY),
        ("d LEFT JOIN f ON d.c0 = f.c1 WHERE f.rn = 1", _D_KEY),
        # With two sources in scope, an unqualified rn could name either one.
        ("d JOIN f ON d.c0 = f.c1 WHERE rn = 1", _NO_KEYS),
        # A rank that is not pinned to the top leaves several f rows per d row.
        ("d JOIN f ON d.c0 = f.c1 AND f.rn = 2", _NO_KEYS),
        ("d JOIN f ON d.c0 = f.c1 AND f.rn > 1", _NO_KEYS),
        ("d JOIN f ON d.c0 = f.c1", _NO_KEYS),
        ("d LEFT JOIN f ON d.c0 = f.c1", _NO_KEYS),
        ("d LEFT JOIN f ON d.c0 = f.c1 WHERE f.rn IS NULL", _NO_KEYS),
        ("d LEFT JOIN f ON d.c0 = f.c1 WHERE f.rn = 1 OR d.c0 = 0", _NO_KEYS),
        ("d JOIN f ON d.c0 = f.c1 AND (f.rn = 1 OR d.c0 = 0)", _NO_KEYS),
        # RIGHT and FULL preserve f, so an ON conjunct on f never filters its rows.
        ("d RIGHT JOIN f ON d.c0 = f.c1 AND f.rn = 1", _NO_KEYS),
        ("d FULL JOIN f ON d.c0 = f.c1 AND f.rn = 1", _NO_KEYS),
        # The ON conjunct that names the other side filters nothing on f.
        ("d JOIN f ON d.c0 = f.c1 AND d.c0 = 1", _NO_KEYS),
    ],
)
def test_the_filter_site_decides_whether_the_window_key_activates(
    tail: str, expected: frozenset[Key]
) -> None:
    assert _joined(tail) == expected


def test_a_filter_inside_the_same_model_cte_activates_the_key() -> None:
    sql = f"WITH f AS ({_window()}) SELECT {_COLS} FROM f WHERE rn = 1"
    assert _promoted("m", _model("m", sql)) == {frozenset({"c1"})}


def test_a_filter_on_an_unrelated_alias_activates_nothing() -> None:
    sql = (
        f"WITH f AS ({_window()}), g AS (SELECT c0 AS g0, c1 AS g1 FROM events) "
        "SELECT f.c0, f.c1, f.c2 FROM f JOIN g ON f.c0 = g.g0 WHERE g.g1 = 1"
    )
    assert _promoted("m", _model("m", sql)) == frozenset()
