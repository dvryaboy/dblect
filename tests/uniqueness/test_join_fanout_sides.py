"""join_fanout blames the side a join repeats and only when a consumer reads it (#305)."""

from __future__ import annotations

import functools
import itertools
import re
from collections.abc import Mapping

import pytest
from sqlglot import Expr

from dblect.sql import parse_sql
from dblect.sql._sqlglot import JoinSide
from dblect.uniqueness.detector import detect_join_fanout

_Keys = Mapping[str, frozenset[frozenset[str]]]
_SIDES = (JoinSide.INNER, JoinSide.LEFT, JoinSide.RIGHT, JoinSide.FULL)


def _keys(**by_relation: tuple[tuple[str, ...], ...]) -> _Keys:
    return {n: frozenset(frozenset(k) for k in ks) for n, ks in by_relation.items()}


def _select(consumer: str, source: str) -> str:
    """``consumer`` is a projection, optionally with a trailing ``group by``."""
    projection, _, grouping = consumer.partition(" group by ")
    return f"select {projection} from {source}" + (f" group by {grouping}" if grouping else "")


@functools.cache
def _parsed(sql: str) -> Expr:
    return parse_sql(sql, dialect="duckdb")


def _fires(sql: str, keys: _Keys, declared: tuple[str, ...] = ()) -> bool:
    key_set: frozenset[frozenset[str]] = frozenset([frozenset(declared)] if declared else [])
    return bool(detect_join_fanout(_parsed(sql), model_keys=keys, declared_keys=key_set))


# Joining on orders.customer_id repeats customers and leaves orders single.
_KEYED = _keys(
    customers=(("customer_id",),),
    orders=(("order_id",),),
    items=(("item_id",),),
    regions=(("region",),),
    covered=(("k1", "k2"),),
    loose=(("row_id",),),
)
_TO_ORDERS = "customers c join orders o on o.customer_id = c.customer_id"
_TO_ITEMS = f"{_TO_ORDERS} join items i on i.order_id = o.order_id"
_SPAN = (
    "customers c join regions r on r.region = c.region "
    "join {} t on t.k1 = c.customer_id and t.k2 = r.region"
)

_CONSUMERS: tuple[tuple[str, bool], ...] = (
    ("sum(c.credit)", True),
    ("sum(o.amount)", False),
    ("sum(c.credit * o.amount)", True),
    ("count(c.customer_id)", True),
    ("count(*)", False),
    ("count(distinct c.customer_id)", False),
    ("c.name, sum(o.amount) group by c.name", False),
    # each group is one order row, hence one customer row: nothing to over-count
    ("o.order_id, sum(c.credit) group by o.order_id", False),
    ("c.customer_id, sum(c.credit) group by c.customer_id", True),
    # an unqualified grouping column names no side, so the collapse is not proven
    ("order_id, sum(c.credit) group by order_id", True),
    ("sum(c.credit) over (partition by o.order_id)", True),
    ("sum(credit)", True),
    ("sum(C.credit)", True),
    ("sum(O.amount)", False),
    ("sum(1)", True),
    ("c.name", True),
    ("c.*", True),
    ("c.name, o.amount", False),
    ("*", False),
    ("distinct c.name", False),
)


@pytest.mark.parametrize(("consumer", "fires"), _CONSUMERS)
def test_consumer_fires_only_when_it_reads_a_repeated_side(consumer: str, fires: bool) -> None:
    assert _fires(_select(consumer, _TO_ORDERS), _KEYED) is fires


_ON_CUSTOMER = "o.customer_id = c.customer_id"
_SUB_FIRST = f"(select * from orders) o join customers c on {_ON_CUSTOMER}"
_SUB_LAST = f"customers c join (select * from orders) o on {_ON_CUSTOMER}"

_SCENARIOS: tuple[tuple[str, str, bool, tuple[str, ...]], ...] = (
    # many-to-many repeats both sides, so a row count matches neither
    ("customers c join orders o on c.region = o.region", "count(*)", True, ()),
    ("customers c join orders o on c.region = o.region", "sum(o.amount)", True, ()),
    # a fan-out repeats every side to its left
    (_TO_ITEMS, "sum(c.credit)", True, ()),
    (_TO_ITEMS, "sum(o.amount)", True, ()),
    (_TO_ITEMS, "sum(i.qty)", False, ()),
    # a repeated side repeats what joins onward through it
    (f"{_TO_ORDERS} join regions r on r.region = c.region", "sum(r.population)", True, ()),
    # an ON spanning two left sides: t pins one row of each, but may match many (c, r) pairs
    (_SPAN.format("covered"), "sum(c.credit)", False, ()),
    (_SPAN.format("loose"), "sum(t.v)", False, ()),
    (_SPAN.format("loose"), "sum(c.credit)", True, ()),
    # a declared key fires when its sides repeat together in a chain
    (_TO_ITEMS, "c.customer_id, o.order_id, i.item_id", True, ("customer_id",)),
    (_TO_ITEMS, "c.customer_id, o.order_id, i.item_id", True, ("order_id",)),
    (_TO_ITEMS, "c.customer_id, o.order_id, i.item_id", False, ("item_id",)),
    # CROSS, SEMI, ANTI and the IS NULL idiom mid-chain add no side and leave the rest readable
    (f"{_TO_ORDERS} cross join regions r", "sum(c.credit)", True, ()),
    (f"{_TO_ORDERS} cross join regions r", "sum(r.population)", False, ()),
    (f"{_TO_ORDERS} semi join regions r on r.region = c.region", "sum(c.credit)", True, ()),
    (f"{_TO_ORDERS} semi join regions r on r.region = c.region", "sum(o.amount)", False, ()),
    (f"{_TO_ORDERS} anti join regions r on r.region = c.region", "sum(c.credit)", True, ()),
    (f"{_TO_ORDERS} anti join regions r on r.region = c.region", "sum(o.amount)", False, ()),
    (
        f"{_TO_ORDERS} left join regions r on r.region = c.region where r.region is null",
        "sum(c.credit)",
        True,
        (),
    ),
    # a subquery side decides like the table it wraps, in either spelling
    (_SUB_FIRST, "sum(c.credit)", True, ()),
    (_SUB_LAST, "sum(c.credit)", True, ()),
    (_SUB_LAST, "sum(o.amount)", False, ()),
)


@pytest.mark.parametrize(("source", "consumer", "fires", "declared"), _SCENARIOS)
def test_many_to_many_and_chains(
    source: str, consumer: str, fires: bool, declared: tuple[str, ...]
) -> None:
    assert _fires(_select(consumer, source), _KEYED, declared) is fires


_JOINED_ROWS = f"select c.customer_id, c.credit, o.amount from {_TO_ORDERS}"


@pytest.mark.parametrize(
    ("sql", "fires"),
    [
        # a join that only passes rows on is judged by whoever consumes them
        (f"with j as ({_JOINED_ROWS}) select sum(credit) from j", True),
        (f"select sum(credit) from ({_JOINED_ROWS}) j", True),
        (f"with j as ({_JOINED_ROWS}) select * from j", False),
        (f"with j as ({_JOINED_ROWS}) select credit from j", True),
        (f"{_JOINED_ROWS} union all {_JOINED_ROWS}", False),
        (f"with j as ({_JOINED_ROWS} union all {_JOINED_ROWS}) select * from j", True),
        (f"with j as ({_JOINED_ROWS.replace('select', 'select distinct')}) select * from j", False),
    ],
)
def test_a_join_whose_rows_another_scope_reads_is_judged_by_that_scope(
    sql: str, fires: bool
) -> None:
    assert _fires(sql, _KEYED) is fires


# --- a CTE or subquery join is judged where its rows are consumed (#324) ---

_LINES_KEYED = _keys(lines=(("claim_id", "line"),), claims=(("claim_id",),))
_LOOKUP = "lines l left join claims c on l.claim_id = c.claim_id"


@pytest.mark.parametrize("projection", ["l.claim_id, l.line, c.dx", "l.claim_id, l.line, c.amt"])
def test_a_lookup_join_is_silent_in_a_cte_as_at_the_top_level(projection: str) -> None:
    direct = _fires(f"select {projection} from {_LOOKUP}", _LINES_KEYED)
    wrapped = _fires(
        f"with final as (select {projection} from {_LOOKUP}) select * from final", _LINES_KEYED
    )
    assert not direct
    assert not wrapped


def test_a_cte_still_fires_when_a_downstream_aggregate_sums_the_repeated_side() -> None:
    sql = f"with final as (select l.claim_id, l.line, c.amt from {_LOOKUP}) select sum(amt) from final"
    assert _fires(sql, _LINES_KEYED)


# The joined rows, projected under distinct names so a downstream scope can read any of them.
_PROJECTED = "c.customer_id, c.name, c.credit, o.order_id, o.amount"
_WRAPPED_ROWS = f"select {_PROJECTED} from {_TO_ORDERS}"


def _unqualified(consumer: str) -> str:
    return re.sub(r"\b[coCO]\.", "", consumer)


# Each shape the direct-select table decides is decided the same way one scope downstream.
# c.* has no unqualified spelling, and the grouped-by-key collapse is not followed downstream.
_NOT_FOLLOWED = {"c.*", "o.order_id, sum(c.credit) group by o.order_id"}


@pytest.mark.parametrize(
    ("consumer", "fires"), [(c, f) for c, f in _CONSUMERS if c not in _NOT_FOLLOWED]
)
@pytest.mark.parametrize("wrap", ["cte", "subquery"])
def test_a_consumer_downstream_of_the_join_decides_as_it_does_on_the_join(
    consumer: str, fires: bool, wrap: str
) -> None:
    downstream = _unqualified(consumer)
    if wrap == "cte":
        sql = f"with j as ({_WRAPPED_ROWS}) {_select(downstream, 'j')}"
    else:
        sql = _select(downstream, f"({_WRAPPED_ROWS}) j")
    assert _fires(sql, _KEYED) is fires


_CHAIN = f"with j as ({_WRAPPED_ROWS})"

_TOPOLOGIES: tuple[tuple[str, bool], ...] = (
    # a pass-through CTE chain carries the repeated side to the aggregate
    (f"{_CHAIN}, k as (select credit as v from j) select sum(v) from k", True),
    (f"{_CHAIN}, k as (select amount as v from j) select sum(v) from k", False),
    (f"{_CHAIN}, k as (select * from j), m as (select * from k) select sum(credit) from m", True),
    (f"{_CHAIN}, k as (select * from j), m as (select * from k) select sum(amount) from m", False),
    # a computed column reads every side its expression names
    (f"{_CHAIN}, k as (select credit * amount as v from j) select sum(v) from k", True),
    (f"{_CHAIN}, k as (select credit * amount as v from j) select v from k", False),
    # a collapse downstream ends the hazard for rows
    (f"{_CHAIN}, k as (select distinct credit from j) select credit from k", False),
    (f"{_CHAIN}, k as (select name, sum(amount) as t from j group by name) select t from k", False),
    # a constant names no side, so an aggregate of it is not proven safe
    (f"with j as (select 1 as one, c.name from {_TO_ORDERS}) select sum(one) from j", True),
    # one hurtful reader is enough when the CTE is read twice
    (f"{_CHAIN} select amount from j union all select credit from j", True),
    (f"{_CHAIN} select amount from j union all select amount from j", False),
    # a reader that joins the CTE again or a union body is not followed; a CTE nobody reads
    # has no consumer
    (f"{_CHAIN} select sum(amount) from j join regions r on r.region = j.name", True),
    (f"with j as ({_WRAPPED_ROWS} union all {_WRAPPED_ROWS}) select amount from j", True),
    (f"{_CHAIN} select 1 as x", False),
    # nested subqueries follow the same path
    (f"select sum(credit) from (select credit from ({_WRAPPED_ROWS}) j) k", True),
    (f"select sum(amount) from (select amount from ({_WRAPPED_ROWS}) j) k", False),
)


@pytest.mark.parametrize(("sql", "fires"), _TOPOLOGIES)
def test_the_rows_of_a_cte_are_followed_through_the_scopes_that_read_them(
    sql: str, fires: bool
) -> None:
    assert _fires(sql, _KEYED) is fires


@pytest.mark.parametrize("known", ["orders", "customers"])
def test_a_side_without_known_keys_is_never_blamed(known: str) -> None:
    keys = _keys(**{known: (("order_id",) if known == "orders" else ("customer_id",),)})
    source = "customers c join orders o on o.region = c.region"
    assert _fires(_select("sum(o.amount)", source), keys) is (known == "customers")
    assert _fires(_select("sum(c.credit)", source), keys) is (known == "orders")


# A declared key on the model reads a plain projection as intent.


@pytest.mark.parametrize("customers_first", [True, False])
@pytest.mark.parametrize(
    ("declared", "projection", "fires"),
    [
        (("customer_id",), "c.customer_id, c.name, o.amount", True),
        (("cid",), "c.customer_id as cid, o.amount", True),
        (("customer_id",), "c.*, o.amount", True),
        (("order_id",), "c.customer_id, o.order_id, o.amount", False),
        (("customer_id", "order_id"), "c.customer_id, o.order_id", False),
        (("customer_id",), "customer_id, o.amount", False),
        (("customer_id",), "distinct c.customer_id, o.amount", False),
        (("customer_id",), "c.customer_id, sum(o.amount) group by c.customer_id", False),
    ],
)
def test_a_declared_key_read_only_from_a_repeated_side_fires(
    declared: tuple[str, ...], projection: str, fires: bool, customers_first: bool
) -> None:
    on = "o.customer_id = c.customer_id"
    source = _TO_ORDERS if customers_first else f"orders o join customers c on {on}"
    assert _fires(_select(projection, source), _KEYED, declared) is fires


@pytest.mark.parametrize(
    "tail",
    [
        "qualify row_number() over (partition by c.customer_id order by o.order_id) = 1",
        "where o.order_id = 'first'",
    ],
)
def test_a_declared_key_the_scope_already_establishes_is_not_judged(tail: str) -> None:
    sql = f"select c.customer_id, o.amount from {_TO_ORDERS} {tail}"
    assert not _fires(sql, _KEYED, ("customer_id",))


# --- exhaustive oracle: which side repeats, over every small key-respecting instance ---

_COLS = 3
_KeySpec = tuple[tuple[int, ...], ...]
_On = tuple[tuple[int, int], ...]
_KEY_SPECS: tuple[_KeySpec, ...] = (((0,),), ((0, 1),), ((0,), (1, 2)))


def _on_specs() -> list[_On]:
    """Every equality conjunction pairing distinct columns of a with distinct columns of b."""
    return [
        tuple(zip(a, b, strict=True))
        for n in range(1, _COLS + 1)
        for a in itertools.combinations(range(_COLS), n)
        for b in itertools.permutations(range(_COLS), n)
    ]


def _tables(keys: _KeySpec) -> list[tuple[tuple[int, ...], ...]]:
    rows = list(itertools.product((0, 1), repeat=_COLS))
    return [
        t
        for n in range(3)
        for t in itertools.combinations(rows, n)
        if all(len({tuple(r[i] for i in k) for r in t}) == len(t) for k in keys)
    ]


def _repeats(a_keys: _KeySpec, b_keys: _KeySpec, on: _On) -> tuple[bool, bool]:
    """Whether some row of a (resp. b) matches two rows of the other side, over all instances."""
    a_rep = b_rep = False
    for ta, tb in itertools.product(_tables(a_keys), _tables(b_keys)):
        matches = [[all(ra[i] == rb[j] for i, j in on) for rb in tb] for ra in ta]
        a_rep |= any(sum(row) > 1 for row in matches)
        b_rep |= any(sum(col) > 1 for col in zip(*matches, strict=True))
    return a_rep, b_rep


def _names(keys: _KeySpec) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(f"k{i}" for i in k) for k in keys)


def _two_tables(on: _On, side: JoinSide, *, a_first: bool, consumer: str) -> str:
    cond = " and ".join(f"a.k{i} = b.k{j}" for i, j in on)
    source = (
        f"ta a {side.value} join tb b on {cond}"
        if a_first
        else f"tb b {side.value} join ta a on {cond}"
    )
    return _select(consumer, source)


@pytest.mark.parametrize("a_first", [True, False])
@pytest.mark.parametrize("side", _SIDES)
def test_detector_agrees_with_the_enumerated_oracle(side: JoinSide, a_first: bool) -> None:
    for a_keys, b_keys in itertools.product(_KEY_SPECS, repeat=2):
        keys = _keys(ta=_names(a_keys), tb=_names(b_keys))
        for on in _on_specs():
            expected = _repeats(a_keys, b_keys, on)
            got = tuple(
                _fires(_two_tables(on, side, a_first=a_first, consumer=f"sum({x}.k2)"), keys)
                for x in "ab"
            )
            assert got == expected, (a_keys, b_keys, on, side, a_first)


# --- the verdict does not depend on which table is written first ---

_SYMMETRY_CONSUMERS = ("sum(a.k2)", "count(*)", "a.k0")
_MIRROR = {JoinSide.LEFT: JoinSide.RIGHT, JoinSide.RIGHT: JoinSide.LEFT}


def test_the_verdict_is_independent_of_join_order() -> None:
    specs = (None, *_KEY_SPECS)  # None: no keys known
    for a_keys, b_keys, on, side, consumer in itertools.product(
        specs, specs, _on_specs(), _SIDES, _SYMMETRY_CONSUMERS
    ):
        named = {"ta": a_keys, "tb": b_keys}
        keys = _keys(**{n: _names(k) for n, k in named.items() if k is not None})
        forward = _fires(_two_tables(on, side, a_first=True, consumer=consumer), keys)
        # `a left join b` is `b right join a`, so the mirrored spelling swaps the outer side too.
        mirrored = _MIRROR.get(side, side)
        backward = _fires(_two_tables(on, mirrored, a_first=False, consumer=consumer), keys)
        assert forward is backward, (a_keys, b_keys, on, side, consumer)


_Edge = tuple[str, str, str]
_Chain = tuple[Mapping[str, str], tuple[_Edge, ...], _Keys]
_CHAINS: tuple[_Chain, ...] = (
    (
        {"c": "customers c", "o": "orders o", "i": "items i"},
        (("c", "o", _ON_CUSTOMER), ("o", "i", "i.order_id = o.order_id")),
        _KEYED,
    ),
    # f has no known keys, so it is nobody's claim in any order
    (
        {"f": "f", "d": "d", "e": "e"},
        (("f", "d", "d.x = f.x"), ("d", "e", "e.y = d.y")),
        _keys(d=(("y",),), e=(("id",),)),
    ),
)


def _chain_in_order(
    order: tuple[str, ...], tables: Mapping[str, str], edges: tuple[_Edge, ...]
) -> str | None:
    """The chain written in ``order``, each ON at its later end; ``None`` if a table joins in
    before anything connects it."""
    source = tables[order[0]]
    for position, alias in enumerate(order[1:], 1):
        before = {*order[:position], alias}
        ons = [on for a, b, on in edges if alias in (a, b) and {a, b} <= before]
        if not ons:
            return None
        source += f" join {tables[alias]} on {' and '.join(ons)}"
    return source


@pytest.mark.parametrize(("tables", "edges", "keys"), _CHAINS)
def test_a_three_table_chain_has_one_verdict_in_every_join_order(
    tables: Mapping[str, str], edges: tuple[_Edge, ...], keys: _Keys
) -> None:
    for consumer in [*(f"sum({alias}.v)" for alias in tables), "count(*)", "*"]:
        verdicts = {
            order: _fires(_select(consumer, source), keys)
            for order in itertools.permutations(tables)
            if (source := _chain_in_order(order, tables, edges)) is not None
        }
        assert len(set(verdicts.values())) == 1, (consumer, verdicts)
