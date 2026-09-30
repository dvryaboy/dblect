"""``detect_join_fanout`` decides which side of a join is multiplied, then asks whether a
duplicate-sensitive consumer reads it (issue #305).

Two properties pin the contract. The oracle test enumerates every small key-respecting
instance and checks the detector against what the join really does to a side's rows. The
symmetry property checks that writing the same inner join the other way round yields the
same verdict.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Mapping, Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlglot import Expr

from dblect.sql import FindingKind, parse_sql
from dblect.sql._sqlglot import JoinSide
from dblect.uniqueness.detector import detect_join_fanout

_Keys = Mapping[str, frozenset[frozenset[str]]]

# Sides that can repeat a source row through matching. CROSS, SEMI and ANTI are skipped by
# design and have their own tests.
_MATCHING_SIDES = (JoinSide.INNER, JoinSide.LEFT, JoinSide.RIGHT, JoinSide.FULL)
_SKIPPED_SIDES = (JoinSide.CROSS, JoinSide.SEMI, JoinSide.ANTI)


def _keys(**by_relation: tuple[tuple[str, ...], ...]) -> _Keys:
    return {n: frozenset(frozenset(k) for k in ks) for n, ks in by_relation.items()}


def _parse(sql: str) -> Expr:
    return parse_sql(sql, dialect="duckdb")


def _fires(sql: str, keys: _Keys) -> bool:
    findings = detect_join_fanout(_parse(sql), model_keys=keys)
    assert all(f.kind is FindingKind.JOIN_FANOUT for f in findings)
    return bool(findings)


def _select(consumer: str, source: str) -> str:
    """``consumer`` is a projection, optionally followed by ``group by``, which must trail FROM."""
    projection, _, grouping = consumer.partition(" group by ")
    return f"select {projection} from {source}" + (f" group by {grouping}" if grouping else "")


def _keyword(side: JoinSide) -> str:
    return "join" if side is JoinSide.INNER else f"{side.value} join"


# --- the consumer space, over one-to-many, many-to-many and one-to-one joins ---------------

# customers is unique on id, orders on order_id. Joining on orders.customer_id leaves the
# customers side multiplied (an order matches one customer, a customer matches many orders)
# and the orders side single.
_KEYED = _keys(customers=(("id",),), orders=(("order_id",),))
_ON_CUSTOMER = "o.customer_id = c.id"

# (projection tail, fires): the tail follows ``select`` and may carry its own GROUP BY.
_ONE_TO_MANY_CONSUMERS: tuple[tuple[str, bool], ...] = (
    ("sum(c.credit)", True),
    ("avg(c.credit)", True),
    ("sum(o.amount)", False),
    ("avg(o.amount)", False),
    ("sum(c.credit * o.amount)", True),
    ("count(c.id)", True),
    ("count(o.order_id)", False),
    ("count(*)", False),
    ("count(1)", False),
    ("count(distinct c.id)", False),
    ("max(c.credit)", False),
    ("c.id, sum(o.amount) group by c.id", False),
    ("c.name, sum(o.amount) group by c.name", False),
    ("o.order_id, sum(c.credit) group by o.order_id", True),
    ("sum(c.credit) over (partition by o.order_id)", True),
    ("sum(o.amount) over (partition by c.id)", False),
    ("c.name", True),
    ("c.*", True),
    ("c.name, o.amount", False),
    ("*", False),
    ("distinct c.name", False),
    ("sum(credit)", True),
)


@pytest.mark.parametrize("probe_first", [True, False], ids=["customers_first", "orders_first"])
@pytest.mark.parametrize("side", _MATCHING_SIDES)
@pytest.mark.parametrize(("consumer", "fires"), _ONE_TO_MANY_CONSUMERS)
def test_one_to_many_fires_only_when_the_consumer_reads_the_multiplied_side(
    consumer: str, fires: bool, side: JoinSide, probe_first: bool
) -> None:
    join = _keyword(side)
    source = (
        f"customers c {join} orders o on {_ON_CUSTOMER}"
        if probe_first
        else f"orders o {join} customers c on {_ON_CUSTOMER}"
    )
    assert _fires(_select(consumer, source), _KEYED) is fires


# Both sides non-unique on the join column: every row can repeat, so no side's row count
# equals the join's, and COUNT(*) has nothing faithful to count.
_MANY_TO_MANY_CONSUMERS: tuple[tuple[str, bool], ...] = (
    ("sum(c.credit)", True),
    ("sum(o.amount)", True),
    ("count(*)", True),
    ("count(1)", True),
    ("count(distinct c.id)", False),
    ("c.name, o.amount", True),
    ("*", True),
    ("c.name, sum(o.amount) group by c.name", True),
)


def _source(on: str, *, probe_first: bool) -> str:
    return (
        f"customers c join orders o on {on}"
        if probe_first
        else f"orders o join customers c on {on}"
    )


@pytest.mark.parametrize("probe_first", [True, False])
@pytest.mark.parametrize(("consumer", "fires"), _MANY_TO_MANY_CONSUMERS)
def test_many_to_many_multiplies_both_sides(consumer: str, fires: bool, probe_first: bool) -> None:
    source = _source("c.region = o.region", probe_first=probe_first)
    assert _fires(_select(consumer, source), _KEYED) is fires


@pytest.mark.parametrize("consumer", [c for c, _ in _MANY_TO_MANY_CONSUMERS])
@pytest.mark.parametrize("probe_first", [True, False])
def test_join_on_both_keys_multiplies_nothing(consumer: str, probe_first: bool) -> None:
    source = _source("c.id = o.order_id", probe_first=probe_first)
    assert not _fires(_select(consumer, source), _KEYED)


@pytest.mark.parametrize("side", _SKIPPED_SIDES)
def test_skipped_join_kinds_stay_silent_for_every_consumer(side: JoinSide) -> None:
    on = "" if side is JoinSide.CROSS else f" on {_ON_CUSTOMER}"
    for consumer, _ in _ONE_TO_MANY_CONSUMERS:
        sql = _select(consumer, f"customers c {side.value} join orders o{on}")
        assert not _fires(sql, _KEYED), consumer


# --- unknown keys: a claim needs a positive fact on the side that would be multiplied ------


def test_unknown_probe_keys_leave_the_joined_in_side_unclaimed() -> None:
    # orders is uncovered, so customers rows repeat. Whether orders rows repeat depends on
    # customers being unique on the join column, which nothing says.
    keys = _keys(orders=(("order_id",),))
    join = "from customers c join orders o on o.customer_id = c.id"
    assert _fires(f"select sum(c.credit) {join}", keys)
    assert not _fires(f"select sum(o.amount) {join}", keys)


def test_unknown_joined_in_keys_leave_the_probe_side_unclaimed() -> None:
    keys = _keys(customers=(("id",),))
    join = "from customers c join orders o on o.region = c.region"
    assert not _fires(f"select sum(c.credit) {join}", keys)
    assert _fires(f"select sum(o.amount) {join}", keys)


# --- chains -------------------------------------------------------------------------------

_CHAIN = _keys(customers=(("id",),), orders=(("order_id",),), items=(("item_id",),))
_CHAIN_FROM = (
    "customers c join orders o on o.customer_id = c.id join items i on i.order_id = o.order_id"
)


@pytest.mark.parametrize(
    ("consumer", "fires"),
    [
        ("sum(c.credit)", True),
        ("sum(o.amount)", True),
        ("sum(i.qty)", False),
        ("count(*)", False),
        ("c.name, o.amount", True),
    ],
)
def test_chained_fan_outs_repeat_every_earlier_row(consumer: str, fires: bool) -> None:
    assert _fires(_select(consumer, _CHAIN_FROM), _CHAIN) is fires


def test_second_join_from_a_repeated_side_repeats_the_new_side() -> None:
    # customers repeat after the first join, so the regions joined onward through customers
    # repeat too, even though regions is unique on the join column.
    keys = _keys(customers=(("id",),), orders=(("order_id",),), regions=(("region",),))
    sql = (
        "select sum(r.population) from customers c join orders o on o.customer_id = c.id "
        "join regions r on r.region = c.region"
    )
    assert _fires(sql, keys)


# An ON predicate spanning two left sides leaves the probe's uniqueness undecided. The joined-in
# side is then blamed only when it is itself uncovered, the case the join has always reported.
_SPANNING_KEYS = _keys(
    customers=(("id",),), regions=(("region",),), covered=(("k1", "k2"),), loose=(("row_id",),)
)
_SPANNING_ON = "t.k1 = c.id and t.k2 = r.region"
_SPANNING_FROM = "customers c join regions r on r.region = c.region join {t} t on " + _SPANNING_ON


def test_undecided_probe_stays_silent_when_the_joined_in_side_is_covered() -> None:
    sql = "select sum(t.v) from " + _SPANNING_FROM.format(t="covered")
    assert not _fires(sql, _SPANNING_KEYS)


def test_undecided_probe_keeps_firing_when_the_joined_in_side_is_uncovered() -> None:
    sql = "select sum(t.v) from " + _SPANNING_FROM.format(t="loose")
    assert _fires(sql, _SPANNING_KEYS)


# --- exhaustive oracle ---------------------------------------------------------------------

_COLUMNS = ("k1", "k2", "k3")
_Row = tuple[int, ...]
_KeySpec = tuple[tuple[str, ...], ...]
_OnSpec = tuple[tuple[str, str], ...]

_KEY_SPECS: tuple[_KeySpec, ...] = (
    (("k1",),),
    (("k1", "k2"),),
    (("k1",), ("k2", "k3")),
)


def _on_specs() -> Iterator[_OnSpec]:
    """Every equality conjunction pairing distinct columns of a with distinct columns of b."""
    for size in (1, 2, 3):
        for a_cols in itertools.combinations(_COLUMNS, size):
            for b_cols in itertools.permutations(_COLUMNS, size):
                yield tuple(zip(a_cols, b_cols, strict=True))


def _bags(max_rows: int) -> list[tuple[_Row, ...]]:
    rows = list(itertools.product((0, 1), repeat=len(_COLUMNS)))
    return [
        bag for n in range(max_rows + 1) for bag in itertools.combinations_with_replacement(rows, n)
    ]


def _respects(bag: Sequence[_Row], keys: _KeySpec) -> bool:
    for key in keys:
        idx = [_COLUMNS.index(c) for c in key]
        projected = [tuple(r[i] for i in idx) for r in bag]
        if len(set(projected)) != len(projected):
            return False
    return True


def _repeats(a: Sequence[_Row], b: Sequence[_Row], on: _OnSpec) -> tuple[bool, bool]:
    """Whether some row of ``a`` (resp. ``b``) joins to two or more rows of the other side."""
    pairs = [
        (i, j)
        for i, ra in enumerate(a)
        for j, rb in enumerate(b)
        if all(ra[_COLUMNS.index(ca)] == rb[_COLUMNS.index(cb)] for ca, cb in on)
    ]
    a_count = [sum(1 for i, _ in pairs if i == n) for n in range(len(a))]
    b_count = [sum(1 for _, j in pairs if j == n) for n in range(len(b))]
    return any(c > 1 for c in a_count), any(c > 1 for c in b_count)


def _oracle(a_keys: _KeySpec, b_keys: _KeySpec, on: _OnSpec) -> tuple[bool, bool]:
    """Over every key-respecting pair of small tables, whether ``a``'s rows (resp. ``b``'s)
    can appear in more than one output row. Two rows per table witness every case: one row
    matching two, or two rows matching one."""
    bags = _bags(2)
    a_bags = [x for x in bags if _respects(x, a_keys)]
    b_bags = [x for x in bags if _respects(x, b_keys)]
    a_rep = b_rep = False
    for xa in a_bags:
        for xb in b_bags:
            ar, br = _repeats(xa, xb, on)
            a_rep, b_rep = a_rep or ar, b_rep or br
        if a_rep and b_rep:
            break
    return a_rep, b_rep


def _spell(on: _OnSpec, side: JoinSide, *, a_first: bool, consumer: str) -> str:
    cond = " and ".join(f"a.{ca} = b.{cb}" for ca, cb in on)
    source = (
        f"ta a {_keyword(side)} tb b on {cond}"
        if a_first
        else f"tb b {_keyword(side)} ta a on {cond}"
    )
    return _select(consumer, source)


@pytest.mark.parametrize("a_first", [True, False], ids=["a_first", "b_first"])
@pytest.mark.parametrize("side", _MATCHING_SIDES)
def test_detector_agrees_with_the_enumerated_oracle(side: JoinSide, a_first: bool) -> None:
    for a_keys, b_keys in itertools.product(_KEY_SPECS, repeat=2):
        keys = _keys(ta=a_keys, tb=b_keys)
        for on in _on_specs():
            a_rep, b_rep = _oracle(a_keys, b_keys, on)
            got_a = _fires(_spell(on, side, a_first=a_first, consumer="sum(a.k3)"), keys)
            got_b = _fires(_spell(on, side, a_first=a_first, consumer="sum(b.k3)"), keys)
            assert (got_a, got_b) == (a_rep, b_rep), (a_keys, b_keys, on, side, a_first)


# --- symmetry property --------------------------------------------------------------------

_SYMMETRY_CONSUMERS = (
    "sum(a.k3)",
    "sum(b.k3)",
    "sum(a.k3 * b.k3)",
    "count(*)",
    "count(a.k1)",
    "a.k1, sum(b.k3) group by a.k1",
    "b.k1, sum(a.k3) group by b.k1",
    "a.k1",
    "a.k1, b.k1",
    "*",
    "sum(b.k3) over (partition by a.k1)",
)

# None models a relation the project knows no keys for.
_key_specs = st.one_of(st.none(), st.sampled_from(_KEY_SPECS))
_on_choice = st.sampled_from(sorted(_on_specs()))
# `a left join b` is `b right join a`: the mirrored spelling swaps the outer side too.
_MIRROR = {
    JoinSide.INNER: JoinSide.INNER,
    JoinSide.FULL: JoinSide.FULL,
    JoinSide.LEFT: JoinSide.RIGHT,
    JoinSide.RIGHT: JoinSide.LEFT,
}


@given(
    a_keys=_key_specs,
    b_keys=_key_specs,
    on=_on_choice,
    side=st.sampled_from(_MATCHING_SIDES),
    consumer=st.sampled_from(_SYMMETRY_CONSUMERS),
)
def test_the_verdict_is_independent_of_join_order(
    a_keys: _KeySpec | None,
    b_keys: _KeySpec | None,
    on: _OnSpec,
    side: JoinSide,
    consumer: str,
) -> None:
    named = {"ta": a_keys, "tb": b_keys}
    keys = _keys(**{n: k for n, k in named.items() if k is not None})
    forward = _fires(_spell(on, side, a_first=True, consumer=consumer), keys)
    backward = _fires(_spell(on, _MIRROR[side], a_first=False, consumer=consumer), keys)
    assert forward is backward
