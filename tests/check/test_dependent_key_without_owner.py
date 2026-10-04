# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance; annotating it that way trips pyright's self-supertype rule while keeping
# the proxy usage checked. Typed ``self`` in authored contracts is the stubs concern.
"""The dependent-key-without-owner check end to end.

``order_lines.line_no`` is declared ``unique_per(order_id)``: a line number means
something only inside its order. A GROUP BY, or one join's ON equalities, that reaches
the relation through ``line_no`` without covering ``order_id`` treats lines of different
orders as the same entity. Using the owners alone, or a composite key with no such
declaration, never fires.

The decision is a closed one (which columns are used, which are pinned), so the
exhaustive tests enumerate every subset of a three-column relation against an independent
set-arithmetic expectation. The table covers the idioms the arithmetic does not see: CTE
and subquery reads, renames, and the rollup and parent-join shapes that made the earlier
any-composite-key rule noisy.
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
from dblect.types import ModelContract
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source

_DUCKDB = profile_for_adapter("duckdb")
_KIND = CheckFindingKind.DEPENDENT_KEY_WITHOUT_OWNER

_LINES_COLS = _cols(line_id="INT", order_id="INT", line_no="INT", price="DECIMAL")
_REFUNDS_COLS = _cols(order_id="INT", line_no="INT", refund="DECIMAL")
_ORDERS_COLS = _cols(order_id="INT", status="VARCHAR")
_TRIPLE_COLS = _cols(a="INT", b="INT", c="INT", v="DECIMAL")
_VERSIONED_COLS = _cols(id="INT", valid_from="DATE", valid_to="DATE", v="DECIMAL")


def _leaf(name: str, columns: Mapping[str, Column]) -> tuple[Node, Node]:
    """A model over its own raw source, selecting every column, so only a declaration
    gives it a key."""
    names = ", ".join(columns)
    return (
        _source(f"source.shop.raw.{name}_raw"),
        _node(f"model.shop.{name}", sql=f"select {names} from {name}_raw", columns=columns),
    )


def _project(consumer_sql: str, *, fd: bool = False) -> Manifest:
    """Leaf models plus one consumer. ``fd`` declares ``line_no -> order_id``.
    ``shipments`` has ``c`` unique per ``(a, b)``; ``probe`` is its keyless lookalike and
    ``plain`` carries the same three-column key with no owner declared. ``versions`` is
    keyed by ``(id, valid_from)`` with no owner declared."""

    class OrderLines(ModelContract):
        dbt_model = "order_lines"

        @contract
        def line_no_is_an_ordinal(self: ContractSelf) -> object:
            return self.line_no.unique_per(self.order_id)

        @contract
        def surrogate(self: ContractSelf) -> object:
            return self.key(self.line_id)

        if fd:

            @contract
            def line_fixes_order(self: ContractSelf) -> object:
                return self.line_no.determines(self.order_id)

    class Shipments(ModelContract):
        dbt_model = "shipments"

        @contract
        def c_is_an_ordinal(self: ContractSelf) -> object:
            return self.c.unique_per(self.a, self.b)

    class Plain(ModelContract):
        dbt_model = "plain"

        @contract
        def grain(self: ContractSelf) -> object:
            return self.key(self.a, self.b, self.c)

    class Versions(ModelContract):
        dbt_model = "versions"

        @contract
        def grain(self: ContractSelf) -> object:
            return self.key(self.id, self.valid_from)

    return _manifest(
        *_leaf("order_lines", _LINES_COLS),
        *_leaf("refunds", _REFUNDS_COLS),
        *_leaf("orders", _ORDERS_COLS),
        *_leaf("shipments", _TRIPLE_COLS),
        *_leaf("plain", _TRIPLE_COLS),
        *_leaf("probe", _TRIPLE_COLS),
        *_leaf("versions", _VERSIONED_COLS),
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


_CASES: tuple[_Case, ...] = (
    # fires
    _Case(
        "join-on-the-dependent-alone",
        "select l.order_id, l.line_no, l.price, r.refund from order_lines l "
        f"join {_REFUND_BY_LINE} on r.line_no = l.line_no",
        True,
    ),
    _Case(
        "join-after-a-rename-in-a-cte",
        "with l as (select order_id, line_no as ln, price from order_lines) "
        f"select l.order_id, l.price, r.refund from l join {_REFUND_BY_LINE} on r.line_no = l.ln",
        True,
    ),
    _Case(
        "group-by-the-dependent-alone",
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
        "a-filter-on-another-column-is-not-a-pin",
        "select line_no, sum(price) from order_lines where price > 0 group by line_no",
        True,
    ),
    _Case(
        "a-range-on-the-owner-is-not-a-pin",
        "select line_no, sum(price) from order_lines where order_id > 42 group by line_no",
        True,
    ),
    _Case(
        "one-of-two-owners-missing",
        "select c, a, sum(v) from shipments group by c, a",
        True,
    ),
    # quiet
    _Case(
        "owners-alone-group-by",
        "select order_id, sum(price) from order_lines group by order_id",
        False,
    ),
    _Case(
        "owners-alone-join",
        "select o.status, l.price from order_lines l join orders o on o.order_id = l.order_id",
        False,
    ),
    _Case(
        "owners-plus-dependent",
        "select order_id, line_no, sum(price) from order_lines group by order_id, line_no",
        False,
    ),
    _Case(
        "literal-pin-of-the-owner-in-where",
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
    ),
    _Case(
        "owner-equated-to-a-grouped-column",
        "select o.order_id, l.line_no, sum(l.price) from order_lines l join orders o "
        "on o.order_id = l.order_id group by o.order_id, l.line_no",
        False,
    ),
    _Case(
        "owner-equated-in-the-same-on",
        "select l.order_id, l.price, r.refund from order_lines l join "
        "(select order_id, line_no, refund from refunds) r "
        "on r.line_no = l.line_no and r.order_id = l.order_id",
        False,
    ),
    _Case(
        "join-with-a-pin-on-the-owner",
        "select l.order_id, l.price, r.refund from order_lines l "
        f"join {_REFUND_BY_LINE} on r.line_no = l.line_no where l.order_id = 7",
        False,
    ),
    _Case(
        "owner-read-inside-a-computed-group-expression",
        "select line_no, order_id + 0, sum(price) from order_lines group by line_no, order_id + 0",
        False,
    ),
    _Case(
        "no-owned-column-used",
        "select price, count(*) from order_lines group by price",
        False,
    ),
    _Case(
        "no-declaration-on-the-relation",
        "select line_no, sum(refund) from refunds group by line_no",
        False,
    ),
    # the shapes that made the any-composite-key rule noisy
    _Case(
        "composite-key-without-owner-rollup",
        "select a, b, sum(v) from plain group by a, b",
        False,
    ),
    _Case(
        "composite-key-without-owner-rollup-to-one-column",
        "select c, sum(v) from plain group by c",
        False,
    ),
    _Case(
        "composite-key-without-owner-parent-join",
        "select p.v from probe p join plain s on s.a = p.a and s.b = p.b",
        False,
    ),
    _Case(
        "versioned-table-joined-on-id-plus-a-date-range",
        "select p.v from probe p join versions x on x.id = p.a "
        "and p.v >= x.valid_from and p.v < x.valid_to",
        False,
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
def test_decision_table(case: _Case) -> None:
    assert bool(_findings(_project(case.sql))) is case.fires


def test_a_declared_dependency_from_a_used_column_covers_the_owner() -> None:
    sql = "select line_no, sum(price) from order_lines group by line_no"
    assert len(_findings(_project(sql))) == 1
    assert _findings(_project(sql, fd=True)) == []


def test_the_message_names_the_relation_the_column_and_the_missing_owner() -> None:
    (finding,) = _findings(_project("select line_no, sum(price) from order_lines group by line_no"))
    assert "'order_lines'" in finding.message
    assert "on line_no" in finding.message
    assert "within order_id" in finding.message
    assert finding.line_start == 1


def test_a_declaration_on_a_source_is_read_where_the_model_selects_from_it() -> None:
    class Ledger(ModelContract):
        dbt_model = "raw.ledger"

        @contract
        def entry_no_is_an_ordinal(self: ContractSelf) -> object:
            return self.entry_no.unique_per(self.account)

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
    assert "within account" in finding.message


def test_it_warns_rather_than_errors() -> None:
    (finding,) = _findings(_project("select line_no, sum(price) from order_lines group by line_no"))
    assert severity_of(finding) is Severity.WARN


# --- the closed decision, enumerated -------------------------------------------------

_COLUMNS = ("a", "b", "c")
_OWNERS = frozenset("ab")
_DEPENDENT = "c"


def _subsets(items: tuple[str, ...]) -> Iterator[frozenset[str]]:
    for combo in chain.from_iterable(combinations(items, n) for n in range(1, len(items) + 1)):
        yield frozenset(combo)


# ``used`` is non-empty; ``pinned`` ranges over every subset, the empty one included.
_USED = list(_subsets(_COLUMNS))
_PINNED = [frozenset[str](), *_subsets(_COLUMNS)]
_PAIRS = [(used, pinned) for used in _USED for pinned in _PINNED]


def _expected(used: frozenset[str], pinned: frozenset[str]) -> bool:
    return _DEPENDENT in used and not used | pinned >= _OWNERS


def _ids(pairs: list[tuple[frozenset[str], frozenset[str]]]) -> list[str]:
    return [f"use-{''.join(sorted(u))}-pin-{''.join(sorted(p)) or 'none'}" for u, p in pairs]


def _where(pinned: frozenset[str]) -> str:
    return " where " + " and ".join(f"s.{c} = 1" for c in sorted(pinned)) if pinned else ""


@pytest.mark.parametrize(("used", "pinned"), _PAIRS, ids=_ids(_PAIRS))
def test_group_by_over_every_subset_of_a_three_column_relation(
    used: frozenset[str], pinned: frozenset[str]
) -> None:
    group = ", ".join(f"s.{c}" for c in sorted(used))
    sql = f"select {group}, sum(s.v) from shipments s{_where(pinned)} group by {group}"
    assert bool(_findings(_project(sql))) is _expected(used, pinned)


@pytest.mark.parametrize(("used", "pinned"), _PAIRS, ids=_ids(_PAIRS))
def test_join_over_every_subset_of_a_three_column_relation(
    used: frozenset[str], pinned: frozenset[str]
) -> None:
    on = " and ".join(f"s.{c} = p.{c}" for c in sorted(used))
    sql = f"select p.v from probe p join shipments s on {on}{_where(pinned)}"
    assert bool(_findings(_project(sql))) is _expected(used, pinned)
