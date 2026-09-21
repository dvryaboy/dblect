# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance; annotating it that way trips pyright's self-supertype rule while keeping
# the proxy usage checked. Typed ``self`` in authored contracts is the stubs concern.
"""The referential orphan-drop check end to end.

A declared foreign key turns an ordinary inner join into something checkable: every
child row is expected to match a parent, so a join that silently drops the
unmatched ones is worth reporting. ``orders.region_id`` is declared a foreign key
to ``regions.region_id`` throughout this file; ``fct`` is the model that joins them,
varied per case to exercise every ``JoinSide`` and child position, the guard that
silences an edge an enabled dbt ``relationships`` test already covers, and the
narrowing a composite ``ON`` clause produces.

``test_join_row_effects.py`` pins the per-join row-effect table this check reads;
this file pins the check built on top of it, through the ``run_check`` boundary
these declarations actually flow through.
"""

from __future__ import annotations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.analysis import analyze
from dblect.check import CheckFinding, CheckFindingKind, CheckReport, run_check
from dblect.manifest import Manifest, Node, TestSeverity
from dblect.sql import FindingKind
from dblect.types import ForeignKey, ModelContract
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import relationships_test as _relationships_test
from tests.check._check_table import CheckCase, run_check_case

_DUCKDB = profile_for_adapter("duckdb")

_ORDERS_COLS = _cols(order_id="INT", amount="DECIMAL", region_id="INT", data_source="VARCHAR")
_REGIONS_COLS = _cols(region_id="INT", region_name="VARCHAR", data_source="VARCHAR")


def _orders(sql: str = "select order_id, amount, region_id, data_source from orders_raw") -> Node:
    return _node("model.shop.orders", sql=sql, columns=_ORDERS_COLS)


def _regions() -> Node:
    return _node(
        "model.shop.regions",
        sql="select region_id, region_name, data_source from regions_raw",
        columns=_REGIONS_COLS,
    )


def _declare_orders_fk() -> None:
    class Orders(ModelContract):
        dbt_model = "orders"
        region_id: ForeignKey("regions.region_id")


def _consumer(sql: str) -> Node:
    return _node("model.shop.fct", sql=sql, columns=_cols(x="INT"))


def _other_thing() -> Node:
    return _node(
        "model.shop.other_thing",
        sql="select region_name, z from other_thing_raw",
        columns=_cols(region_name="VARCHAR", z="INT"),
    )


def _manifest_for(sql: str, *extra: Node, orders: Node | None = None) -> Manifest:
    return _manifest(orders or _orders(), _regions(), _consumer(sql), *extra)


def _run(sql: str, *extra: Node) -> CheckReport:
    return run_check(_manifest_for(sql, *extra), _DUCKDB)


def _orphan_findings(report: CheckReport) -> list[CheckFinding]:
    return [f for f in report.findings if f.kind is CheckFindingKind.REFERENTIAL_ORPHAN_DROP]


# --- structural shapes: join kind x child position, the anti-join idiom, and the
# documented misses, all through one table since they share the orders/regions/fct
# manifest shape and vary only the consumer SQL and (rarely) an extra node. ---------
#
# `_CHILD_LEFT`: orders (child) is the FROM table, regions (parent) is joined in.
# `_CHILD_RIGHT`: regions (parent) is the FROM table, orders (child) is joined in.
# SEMI/ANTI only ever project the probe (accumulated-left) side, so their queries
# project only what that side allows.

_CHILD_LEFT = (
    "select o.order_id, o.amount, r.region_name "
    "from orders o {join} regions r on o.region_id = r.region_id"
)
_CHILD_LEFT_PROBE_ONLY = (
    "select o.order_id, o.amount from orders o {join} regions r on o.region_id = r.region_id"
)
_CHILD_RIGHT = (
    "select r.region_name, o.order_id, o.amount "
    "from regions r {join} orders o on o.region_id = r.region_id"
)
_CHILD_RIGHT_PROBE_ONLY = (
    "select r.region_name from regions r {join} orders o on o.region_id = r.region_id"
)
_FIRING_SQL = _CHILD_LEFT.format(join="inner join")

# id, sql, extra nodes, whether it fires
_StructuralCase = tuple[str, str, tuple[Node, ...], bool]
_STRUCTURAL_CASES: tuple[_StructuralCase, ...] = (
    ("inner-child-left", _CHILD_LEFT.format(join="inner join"), (), True),
    ("left-child-left", _CHILD_LEFT.format(join="left join"), (), False),
    ("right-child-left", _CHILD_LEFT.format(join="right join"), (), True),
    ("full-child-left", _CHILD_LEFT.format(join="full join"), (), False),
    ("semi-child-left", _CHILD_LEFT_PROBE_ONLY.format(join="semi join"), (), True),
    ("anti-child-left", _CHILD_LEFT_PROBE_ONLY.format(join="anti join"), (), False),
    ("inner-child-right", _CHILD_RIGHT.format(join="inner join"), (), True),
    ("left-child-right", _CHILD_RIGHT.format(join="left join"), (), True),
    ("right-child-right", _CHILD_RIGHT.format(join="right join"), (), False),
    ("full-child-right", _CHILD_RIGHT.format(join="full join"), (), False),
    ("semi-child-right", _CHILD_RIGHT_PROBE_ONLY.format(join="semi join"), (), False),
    ("anti-child-right", _CHILD_RIGHT_PROBE_ONLY.format(join="anti join"), (), False),
    (
        # the textbook orphan-finder idiom: surfaces orphans on purpose, so silent
        "left-join-child-probe-is-null-anti-idiom",
        "select o.order_id from orders o left join regions r "
        "on o.region_id = r.region_id where r.region_id is null",
        (),
        False,
    ),
    (
        # find-parents-with-no-children on the child's own key: also the idiom
        "left-join-probe-is-null-on-childs-own-key",
        "select r.region_name from regions r left join orders o "
        "on o.region_id = r.region_id where o.region_id is null",
        (),
        False,
    ),
    (
        # same shape on a non-key column: not the idiom, so left-child-right fires
        "left-join-probe-is-null-on-non-key-column",
        "select r.region_name from regions r left join orders o "
        "on o.region_id = r.region_id where o.amount is null",
        (),
        True,
    ),
    (
        # outside the decided ON fragment (only a top-level conjunction decodes)
        "equality-under-or",
        "select o.order_id, o.amount, r.region_name from orders o inner join regions r "
        "on o.region_id = r.region_id or o.amount > 0",
        (),
        False,
    ),
    (
        # documented miss: sqlglot carries no ON for USING, so it reads as a bare CROSS
        "using-clause",
        "select o.order_id, o.amount from orders o inner join regions r using (region_id)",
        (),
        False,
    ),
    (
        # documented miss: WHERE turns this into an effective INNER; not seen
        "where-on-the-outer-joined-padded-side",
        "select o.order_id, o.amount, r.region_name from orders o left join regions r "
        "on o.region_id = r.region_id where r.region_name = 'active'",
        (),
        False,
    ),
    (
        # documented miss: a later join's ON referencing the padded side
        "later-join-referencing-the-padded-side",
        "select o.order_id, o.amount, x.z from orders o "
        "left join regions r on o.region_id = r.region_id "
        "inner join other_thing x on x.region_name = r.region_name",
        (_other_thing(),),
        False,
    ),
    (
        # documented miss: a comma join's equality lives in WHERE, not ON
        "comma-join-with-equality-in-where",
        "select o.order_id, o.amount from orders o, regions r where o.region_id = r.region_id",
        (),
        False,
    ),
    (
        # the tuva shape: a join inside a CTE body still fires
        "join-inside-a-cte-body",
        "with base as ("
        "  select o.order_id, o.amount, r.region_name from orders o "
        "  inner join regions r on o.region_id = r.region_id"
        ") select * from base",
        (),
        True,
    ),
)


@pytest.mark.parametrize(
    ("case", "extra"),
    [
        (
            CheckCase(
                id_, sql, expected=(CheckFindingKind.REFERENTIAL_ORPHAN_DROP,) if fires else ()
            ),
            extra,
        )
        for id_, sql, extra, fires in _STRUCTURAL_CASES
    ],
    ids=[row[0] for row in _STRUCTURAL_CASES],
)
def test_structural_shape(case: CheckCase, extra: tuple[Node, ...]) -> None:
    _declare_orders_fk()
    run_check_case(case, _manifest_for(case.sql, *extra), _DUCKDB)


# --- the message wording contract --------------------------------------------------


def test_message_names_child_as_preserved_side_and_never_claims_a_violation() -> None:
    _declare_orders_fk()
    (finding,) = _orphan_findings(_run(_FIRING_SQL))
    # The verdict is "not established", never "violated": the analysis is
    # trusting the FK forward, not disproving it (see the design's WARN grade).
    assert "violat" not in finding.message.lower()
    # The remediation names the child relation as the side to preserve rather than
    # prescribing a join keyword: the finding fires on LEFT, RIGHT, and SEMI joins
    # too, where "switch to a LEFT JOIN" is already the case or is the wrong fix.
    assert "switch to a" not in finding.message
    assert "'orders' the preserved side" in finding.message
    assert "also" not in finding.message
    assert "other conditions" not in finding.message


def test_narrowed_on_clause_adds_the_narrowing_sentence() -> None:
    _declare_orders_fk()
    sql = (
        "select o.order_id, o.amount, r.region_name from orders o inner join regions r "
        "on o.region_id = r.region_id and o.data_source = r.data_source"
    )
    (finding,) = _orphan_findings(_run(sql))
    assert "also" in finding.message
    assert "other conditions" in finding.message


# --- edge declaration and test coverage --------------------------------------------
#
# id, declare the contract edge too, the relationships test's non-default kwargs
# (None: no test node at all), whether it fires.
_CoverageCase = tuple[str, bool, dict[str, object] | None, bool]
_COVERAGE_CASES: tuple[_CoverageCase, ...] = (
    ("no-edge-declared", False, None, False),
    ("default-relationships-test-only-edge", False, {}, False),
    ("contract-only-edge", True, None, True),
    ("where-scoped-relationships-test-only-edge", False, {"where": "amount > 0"}, True),
    ("warn-severity-relationships-test-only-edge", False, {"severity": TestSeverity.WARN}, True),
    ("disabled-test-alongside-a-contract-edge", True, {"enabled": False}, True),
)


@pytest.mark.parametrize(
    ("declare_contract", "test_kwargs", "fires"),
    [(dc, kw, fires) for _id, dc, kw, fires in _COVERAGE_CASES],
    ids=[row[0] for row in _COVERAGE_CASES],
)
def test_edge_declaration_and_test_coverage(
    declare_contract: bool, test_kwargs: dict[str, object] | None, fires: bool
) -> None:
    if declare_contract:
        _declare_orders_fk()
    extra = (
        ()
        if test_kwargs is None
        else (
            _relationships_test(
                "test.shop.rel",
                child="model.shop.orders",
                child_column="region_id",
                parent="model.shop.regions",
                parent_column="region_id",
                **test_kwargs,  # type: ignore[arg-type]
            ),
        )
    )
    case = CheckCase(
        "coverage",
        _FIRING_SQL,
        expected=(CheckFindingKind.REFERENTIAL_ORPHAN_DROP,) if fires else (),
    )
    run_check_case(case, _manifest_for(case.sql, *extra), _DUCKDB)


# --- nullable key: both findings fire together -------------------------------------


def test_nullable_key_produces_both_findings_together() -> None:
    # orders.region_id is drawn from the optional side of a LEFT JOIN within
    # orders' own SQL, so it is nullable in orders' output, cross-model, exactly
    # the mechanism `detect_join_on_nullable_key` reads. The two checks answer
    # different questions (a NULL key that never matches vs. a well-formed key
    # with no parent row) and both apply to the same join.
    _declare_orders_fk()
    nullable_orders = _orders(
        sql=(
            "select a.order_id, a.amount, s.region_id, a.data_source "
            "from anchor a left join store s on a.order_id = s.order_id"
        )
    )
    report = analyze(_manifest_for(_FIRING_SQL, orders=nullable_orders), _DUCKDB)
    kinds = {
        f.kind
        for f in report.findings
        if isinstance(f, CheckFinding) and f.model_unique_id == "model.shop.fct"
    }
    assert CheckFindingKind.REFERENTIAL_ORPHAN_DROP in kinds
    audit_kinds = {
        lf.finding.kind for lf in report.audit.findings if lf.model_unique_id == "model.shop.fct"
    }
    assert FindingKind.JOIN_ON_NULLABLE_KEY in audit_kinds
