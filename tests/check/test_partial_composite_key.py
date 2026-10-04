# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance; annotating it that way trips pyright's self-supertype rule while keeping
# the proxy usage checked. Typed ``self`` in authored contracts is the stubs concern.
"""The partial-composite-key check end to end.

``order_lines`` is keyed by ``(order_id, line_no)``: a line number means something only
inside its order. A GROUP BY, or one join's ON equalities, that reaches the relation
through ``line_no`` alone treats lines of different orders as the same entity, and the
numbers come out plausible and wrong. The finding fires when the columns used reach into
a declared composite key without covering it and the missing columns are not pinned.

The decision is a closed one (which key columns are used, which are pinned), so the
exhaustive tests enumerate every subset of a three-column key against an independent
set-arithmetic expectation. The table covers the idioms the arithmetic does not see:
CTE and subquery reads, renames, foreign-key parents, and single-column keys.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import chain, combinations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFinding, CheckFindingKind, run_check
from dblect.contracts import ContractSelf, contract
from dblect.manifest import Column, Manifest, Node
from dblect.severity import Severity, severity_of
from dblect.types import ForeignKey, ModelContract
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source

_DUCKDB = profile_for_adapter("duckdb")
_KIND = CheckFindingKind.PARTIAL_COMPOSITE_KEY

_LINES_COLS = _cols(line_id="INT", order_id="INT", line_no="INT", price="DECIMAL")
_REFUNDS_COLS = _cols(order_id="INT", line_no="INT", refund="DECIMAL")
_ORDERS_COLS = _cols(order_id="INT", status="VARCHAR")
_TRIPLE_COLS = _cols(a="INT", b="INT", c="INT", v="DECIMAL")


def _leaf(name: str, columns: Mapping[str, Column]) -> tuple[Node, Node]:
    """A model over its own raw source, selecting every column, so only a declaration
    gives it a key."""
    names = ", ".join(columns)
    return (
        _source(f"source.shop.raw.{name}_raw"),
        _node(f"model.shop.{name}", sql=f"select {names} from {name}_raw", columns=columns),
    )


def _project(consumer_sql: str, *, fk: bool = False, fd: bool = False) -> Manifest:
    """Keyed leaf models plus one consumer. ``fk`` declares ``order_lines.order_id`` a
    foreign key to ``orders``; ``fd`` declares ``line_no -> order_id``. ``shipments`` is
    keyed by ``(a, b, c)`` and ``probe`` is its keyless lookalike."""

    class OrderLines(ModelContract):
        dbt_model = "order_lines"

        @contract
        def grain(self: ContractSelf) -> object:
            return self.key(self.order_id, self.line_no)

        @contract
        def surrogate(self: ContractSelf) -> object:
            return self.key(self.line_id)

        if fd:

            @contract
            def line_fixes_order(self: ContractSelf) -> object:
                return self.line_no.determines(self.order_id)

    if fk:

        class OrderLinesFk(ModelContract):
            dbt_model = "order_lines"
            order_id: ForeignKey("orders.order_id")

    class Orders(ModelContract):
        dbt_model = "orders"

        @contract
        def grain(self: ContractSelf) -> object:
            return self.key(self.order_id)

    class Shipments(ModelContract):
        dbt_model = "shipments"

        @contract
        def grain(self: ContractSelf) -> object:
            return self.key(self.a, self.b, self.c)

    return _manifest(
        *_leaf("order_lines", _LINES_COLS),
        *_leaf("refunds", _REFUNDS_COLS),
        *_leaf("orders", _ORDERS_COLS),
        *_leaf("shipments", _TRIPLE_COLS),
        *_leaf("probe", _TRIPLE_COLS),
        _node("model.shop.fct", sql=consumer_sql, columns=_cols(x="INT")),
    )


def _findings(manifest: Manifest) -> list[CheckFinding]:
    report = run_check(manifest, _DUCKDB)
    assert report.unbuilt == ()
    return [f for f in report.findings if f.kind is _KIND]


_REFUND_BY_LINE = "(select line_no, sum(refund) as refund from refunds group by line_no) r"


@dataclass(frozen=True, slots=True)
class _Case:
    id: str
    sql: str
    fires: bool
    fk: bool = False
    """``order_lines.order_id`` is declared a foreign key to ``orders``, so a join to the
    parent reads as a reference."""


_CASES: tuple[_Case, ...] = (
    _Case(
        "repro-join-on-the-inner-column-alone",
        "select l.order_id, l.line_no, l.price, r.refund from order_lines l "
        f"join {_REFUND_BY_LINE} on r.line_no = l.line_no",
        True,
    ),
    _Case(
        "rename-in-a-cte",
        "with l as (select order_id, line_no as ln, price from order_lines) "
        f"select l.order_id, l.price, r.refund from l join {_REFUND_BY_LINE} on r.line_no = l.ln",
        True,
    ),
    _Case(
        "group-by-the-inner-column-alone",
        "select line_no, sum(price) from order_lines group by line_no",
        True,
    ),
    _Case(
        "group-by-ordinal",
        "select line_no, sum(price) from order_lines group by 1",
        True,
    ),
    _Case(
        "group-by-through-a-derived-table",
        "select line_no, sum(price) from (select order_id, line_no, price from order_lines) l "
        "group by line_no",
        True,
    ),
    _Case(
        "group-by-an-alias-of-the-column",
        "select line_no as ln, sum(price) from order_lines group by ln",
        True,
    ),
    _Case(
        # the parent id is not declared a foreign key here, so nothing marks it a reference
        "group-by-the-outer-column-with-no-foreign-key",
        "select order_id, sum(price) from order_lines group by order_id",
        True,
    ),
    _Case(
        "a-filter-on-another-column-is-not-a-pin",
        "select line_no, sum(price) from order_lines where price > 0 group by line_no",
        True,
    ),
    _Case(
        "a-range-on-the-missing-column-is-not-a-pin",
        "select line_no, sum(price) from order_lines where order_id > 42 group by line_no",
        True,
    ),
    _Case(
        "group-by-the-full-key",
        "select order_id, line_no, sum(price) from order_lines group by order_id, line_no",
        False,
    ),
    _Case(
        "literal-pin-in-where",
        "select line_no, sum(price) from order_lines where order_id = 42 group by line_no",
        False,
    ),
    _Case(
        "literal-pin-in-a-cte",
        "with l as (select * from order_lines where order_id = 42) "
        "select line_no, sum(price) from l group by line_no",
        False,
    ),
    _Case(
        "literal-pin-in-a-join-side-filter",
        "select l.line_no, sum(l.price) from orders o join order_lines l "
        "on l.order_id = o.order_id and o.order_id = 42 group by l.line_no",
        False,
        fk=True,
    ),
    _Case(
        "missing-column-equated-to-a-grouped-one",
        "select o.order_id, l.line_no, sum(l.price) from order_lines l join orders o "
        "on o.order_id = l.order_id group by o.order_id, l.line_no",
        False,
        fk=True,
    ),
    _Case(
        "join-covering-the-key",
        "select l.order_id, l.price, r.refund from order_lines l join "
        "(select order_id, line_no, refund from refunds) r "
        "on r.line_no = l.line_no and r.order_id = l.order_id",
        False,
    ),
    _Case(
        "join-with-a-pin-on-the-missing-column",
        "select l.order_id, l.price, r.refund from order_lines l "
        f"join {_REFUND_BY_LINE} on r.line_no = l.line_no where l.order_id = 7",
        False,
    ),
    _Case(
        "single-column-key",
        "select order_id, count(*) from orders group by order_id",
        False,
    ),
    _Case(
        "no-key-columns-used",
        "select price, count(*) from order_lines group by price",
        False,
    ),
    _Case(
        "no-key-declared-on-the-relation",
        "select line_no, sum(refund) from refunds group by line_no",
        False,
    ),
    _Case(
        # the missing column is read inside a computed grouping, so it is not left out
        "missing-column-read-inside-a-computed-group-expression",
        "select line_no, order_id + 0, sum(price) from order_lines group by line_no, order_id + 0",
        False,
    ),
    _Case(
        # a surrogate key the clause covers settles it, however the composite key fares
        "another-declared-key-is-covered",
        "select line_id, line_no, sum(price) from order_lines group by line_id, line_no",
        False,
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
def test_decision_table(case: _Case) -> None:
    assert bool(_findings(_project(case.sql, fk=case.fk))) is case.fires


def test_a_declared_dependency_from_a_used_column_covers_the_key() -> None:
    sql = "select line_no, sum(price) from order_lines group by line_no"
    assert len(_findings(_project(sql))) == 1
    assert _findings(_project(sql, fd=True)) == []


def test_a_foreign_key_column_alone_is_a_reference_not_a_partial_key() -> None:
    # Rolling lines up to their order, or joining them to it, is what the foreign
    # key is for.
    rollup = "select order_id, sum(price) from order_lines group by order_id"
    join = "select o.status, l.price from order_lines l join orders o on o.order_id = l.order_id"
    assert len(_findings(_project(rollup))) == 1
    assert _findings(_project(rollup, fk=True)) == []
    assert _findings(_project(join, fk=True)) == []


def test_the_message_names_the_relation_the_key_and_the_missing_columns() -> None:
    (finding,) = _findings(_project("select line_no, sum(price) from order_lines group by line_no"))
    assert "'order_lines'" in finding.message
    assert "(line_no, order_id)" in finding.message
    assert "missing order_id" in finding.message
    assert finding.line_start == 1


def test_a_key_declared_on_a_source_is_read_where_the_model_selects_from_it() -> None:
    class Ledger(ModelContract):
        dbt_model = "raw.ledger"

        @contract
        def grain(self: ContractSelf) -> object:
            return self.key(self.account, self.entry_no)

    ledger = _source(
        "source.shop.raw.ledger", columns=_cols(account="INT", entry_no="INT", amount="DECIMAL")
    )
    fct = _node(
        "model.shop.fct",
        sql="select entry_no, sum(amount) from ledger group by entry_no",
        columns=_cols(x="INT"),
    )
    (finding,) = _findings(_manifest(ledger, fct))
    assert "'ledger'" in finding.message
    assert "missing account" in finding.message


def test_it_warns_rather_than_errors() -> None:
    (finding,) = _findings(_project("select line_no, sum(price) from order_lines group by line_no"))
    assert severity_of(finding) is Severity.WARN


# --- the closed decision, enumerated -------------------------------------------------

_KEY = ("a", "b", "c")


def _subsets(items: tuple[str, ...]) -> Iterator[frozenset[str]]:
    for combo in chain.from_iterable(combinations(items, n) for n in range(1, len(items) + 1)):
        yield frozenset(combo)


# A clause that uses no key column is not a use of the key, so ``used`` is non-empty;
# ``pinned`` ranges over every subset, the empty one included.
_USED = list(_subsets(_KEY))
_PINNED = [frozenset[str](), *_subsets(_KEY)]
_PAIRS = [(used, pinned) for used in _USED for pinned in _PINNED]


def _expected(used: frozenset[str], pinned: frozenset[str]) -> bool:
    return not frozenset(_KEY) <= used | pinned


def _ids(pairs: list[tuple[frozenset[str], frozenset[str]]]) -> list[str]:
    return [f"use-{''.join(sorted(u))}-pin-{''.join(sorted(p)) or 'none'}" for u, p in pairs]


def _where(pinned: frozenset[str]) -> str:
    return " where " + " and ".join(f"s.{c} = 1" for c in sorted(pinned)) if pinned else ""


@pytest.mark.parametrize(("used", "pinned"), _PAIRS, ids=_ids(_PAIRS))
def test_group_by_over_every_subset_of_a_three_column_key(
    used: frozenset[str], pinned: frozenset[str]
) -> None:
    group = ", ".join(f"s.{c}" for c in sorted(used))
    sql = f"select {group}, sum(s.v) from shipments s{_where(pinned)} group by {group}"
    assert bool(_findings(_project(sql))) is _expected(used, pinned)


@pytest.mark.parametrize(("used", "pinned"), _PAIRS, ids=_ids(_PAIRS))
def test_join_over_every_subset_of_a_three_column_key(
    used: frozenset[str], pinned: frozenset[str]
) -> None:
    on = " and ".join(f"s.{c} = p.{c}" for c in sorted(used))
    sql = f"select p.v from probe p join shipments s on {on}{_where(pinned)}"
    assert bool(_findings(_project(sql))) is _expected(used, pinned)
