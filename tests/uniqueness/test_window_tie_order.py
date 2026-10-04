"""Which window functions depend on how ties in the ORDER BY are broken.

Each case is checked two ways: the detector's verdict, and a DuckDB oracle that runs the window
under every physical row order and reports whether any row's value moved. The oracle must never
see a hazard the detector stays silent on.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import duckdb
import pytest

from dblect.lineage.properties.functional_dependency import FD, FDSet
from dblect.sql import FindingKind, parse_sql
from dblect.uniqueness import detect_non_unique_window_order_keys

# (id, a, b, c): ties on `a`; `b` is a function of `a`; `c` differs within a tie.
_ROWS = ((1, 1, 10, 7), (2, 1, 10, 8), (3, 2, 20, 9), (4, 2, 20, 6), (5, 3, 30, 5), (6, 3, 30, 4))
_A_DETERMINES_B = {"src": FDSet.of(FD(frozenset({"a"}), "b"))}
_KEYS = {"src": frozenset({frozenset({"id"})})}

_WHOLE = "rows between unbounded preceding and unbounded following"
_RUNNING = "rows between unbounded preceding and current row"


@dataclass(frozen=True)
class Case:
    window: str
    fires: bool
    hazard: bool  # the oracle sees some row's value move with the tie order
    fires_with_fd: bool | None = None  # set when `a -> b` changes the verdict

    def __str__(self) -> str:
        return self.window


_CASES = (
    # Ranking: tied rows share the value.
    Case("rank() over (order by a)", fires=False, hazard=False),
    Case("dense_rank() over (order by a)", fires=False, hazard=False),
    Case("percent_rank() over (order by a)", fires=False, hazard=False),
    Case("cume_dist() over (order by a)", fires=False, hazard=False),
    # Position-reading: the value depends on where the row lands among its ties. LAG/LEAD of
    # a determined column still move, because a tied row's neighbour is a peer or not.
    Case("row_number() over (order by a)", fires=True, hazard=True),
    Case("ntile(2) over (order by a)", fires=True, hazard=True),
    Case("lag(a) over (order by a)", fires=True, hazard=True),
    Case("lead(a) over (order by a)", fires=True, hazard=True),
    Case("lag(b) over (order by a)", fires=True, hazard=True),
    Case("lead(c) over (order by a)", fires=True, hazard=True),
    # Value functions over the default (peer-closed) frame: silent iff the order determines
    # the argument.
    Case("first_value(a) over (order by a)", fires=False, hazard=False),
    Case("last_value(a) over (order by a)", fires=False, hazard=False),
    Case("nth_value(a, 2) over (order by a)", fires=False, hazard=False),
    Case("first_value(c) over (order by a)", fires=True, hazard=True),
    Case("last_value(c) over (order by a)", fires=True, hazard=True),
    Case("nth_value(c, 2) over (order by a)", fires=True, hazard=True),
    Case("last_value(b) over (order by a)", fires=True, hazard=False, fires_with_fd=False),
    Case("first_value(b) over (order by a)", fires=True, hazard=False, fires_with_fd=False),
    Case("first_value(a ignore nulls) over (order by a)", fires=False, hazard=False),
    Case("last_value(a ignore nulls) over (order by a)", fires=False, hazard=False),
    Case("first_value(c ignore nulls) over (order by a)", fires=True, hazard=True),
    Case("last_value(c ignore nulls) over (order by a)", fires=True, hazard=True),
    # Frames over a value function.
    Case(f"last_value(a) over (order by a {_RUNNING})", fires=True, hazard=False),
    Case(f"last_value(c) over (order by a {_RUNNING})", fires=True, hazard=False),
    Case(f"first_value(a) over (order by a {_WHOLE})", fires=False, hazard=False),
    Case(f"first_value(c) over (order by a {_WHOLE})", fires=True, hazard=True),
    Case(
        "last_value(a) over (order by a groups between current row and current row)",
        fires=False,
        hazard=False,
    ),
    Case(
        "last_value(c) over (order by a range between unbounded preceding and unbounded following)",
        fires=True,
        hazard=True,
    ),
    # Order-insensitive aggregates: only frame membership matters.
    Case("sum(c) over (order by a)", fires=False, hazard=False),
    Case("count(*) over (order by a)", fires=False, hazard=False),
    Case("min(c) over (order by a)", fires=False, hazard=False),
    Case(
        "avg(c) over (order by a range between unbounded preceding and current row)",
        fires=False,
        hazard=False,
    ),
    Case(
        "sum(c) over (order by a groups between 1 preceding and current row)",
        fires=False,
        hazard=False,
    ),
    Case(f"sum(c) over (order by a {_WHOLE})", fires=False, hazard=False),
    Case(f"sum(c) over (order by a {_RUNNING})", fires=True, hazard=True),
    Case("sum(c) over (order by a rows unbounded preceding)", fires=True, hazard=True),
    Case(
        "sum(c) over (order by a rows between 1 preceding and 1 following)", fires=True, hazard=True
    ),
    Case(
        "sum(c) over (order by a range between unbounded preceding and current row"
        " exclude current row)",
        fires=True,
        hazard=False,
    ),
    # Ordered collections read the sequence of their argument.
    Case("array_agg(a) over (order by a)", fires=False, hazard=False),
    Case("array_agg(c) over (order by a)", fires=True, hazard=True),
    Case("array_agg(b) over (order by a)", fires=True, hazard=False, fires_with_fd=False),
    Case("first(a) over (order by a)", fires=False, hazard=False),
    Case("first(c) over (order by a)", fires=True, hazard=True),
    Case("last(c) over (order by a)", fires=True, hazard=True),
    Case("string_agg(cast(a as varchar), ',') over (order by a)", fires=False, hazard=False),
    Case("string_agg(cast(c as varchar), ',') over (order by a)", fires=True, hazard=True),
    # An aggregate type the registry has no entry for is not assumed order-independent.
    Case("corr(a, c) over (order by a)", fires=True, hazard=False),
    # Picks among ties.
    Case("any_value(c) over (order by a)", fires=True, hazard=True),
    Case("arg_max(c, a) over (order by a)", fires=True, hazard=True),
)

# A function the registry does not know keeps the old behaviour: it fires.
_UNKNOWN = "mystery_fn(c) over (order by a)"


def _fires(window: str, fds: dict[str, FDSet], dialect: str = "duckdb") -> bool:
    tree = parse_sql(f"select {window} as w from src", dialect=dialect)
    findings = detect_non_unique_window_order_keys(tree, model_keys=_KEYS, model_fds=fds)
    assert all(f.kind is FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS for f in findings)
    return bool(findings)


@pytest.fixture(scope="module")
def varies_with_tie_order() -> dict[str, bool]:
    """Per window, whether any row's value moves across physical row orders. One query per
    permutation evaluates every window at once."""
    windows = [c.window for c in _CASES]
    select = ", ".join(f"{w} as w{i}" for i, w in enumerate(windows))
    outcomes: list[set[frozenset[tuple[int, str]]]] = [set() for _ in windows]
    con = duckdb.connect()
    for perm in itertools.permutations(_ROWS):
        con.execute("create or replace table src(id int, a int, b int, c int)")
        con.executemany("insert into src values (?, ?, ?, ?)", perm)
        rows = con.execute(f"select id, {select} from src").fetchall()
        for i, seen in enumerate(outcomes):
            seen.add(frozenset((r[0], repr(r[i + 1])) for r in rows))
    con.close()
    return {w: len(seen) > 1 for w, seen in zip(windows, outcomes, strict=True)}


@pytest.mark.parametrize("case", _CASES, ids=str)
def test_detector_verdict(case: Case) -> None:
    assert _fires(case.window, {}) is case.fires
    expected_with_fd = case.fires if case.fires_with_fd is None else case.fires_with_fd
    assert _fires(case.window, _A_DETERMINES_B) is expected_with_fd


@pytest.mark.parametrize(
    ("window", "fires"),
    [
        ("last_value(a) ignore nulls over (order by a)", False),
        ("last_value(c) ignore nulls over (order by a)", True),
        ("first_value(a) over (order by a)", False),
        ("first_value(c) over (order by a)", True),
    ],
)
def test_snowflake_spellings(window: str, fires: bool) -> None:
    """Snowflake puts IGNORE NULLS after the call and sqlglot gives value functions an explicit
    whole-partition frame there."""
    assert _fires(window, {}, dialect="snowflake") is fires


def test_unknown_function_fires() -> None:
    assert _fires(_UNKNOWN, {})
    assert _fires(_UNKNOWN, _A_DETERMINES_B)


@pytest.mark.parametrize("case", _CASES, ids=str)
def test_oracle_agrees_and_detector_is_sound(
    case: Case, varies_with_tie_order: dict[str, bool]
) -> None:
    varies = varies_with_tie_order[case.window]
    assert varies is case.hazard
    if varies:
        assert _fires(case.window, {})
        assert _fires(case.window, _A_DETERMINES_B)
