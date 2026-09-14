# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance; annotating it that way trips pyright's self-supertype rule while keeping
# the proxy usage checked. Typed ``self`` in authored contracts is the stubs concern.
"""The grain check end to end, from a declared contract to the finding. The decision
itself is checked against brute force in ``test_pbt_grain_established.py``.

The first test is the one that matters most: an implementation that compares the
declaration against the merged key set finds the declaration there and never fires.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFinding, CheckFindingKind, CheckReport, run_check
from dblect.contracts import ContractSelf, contract
from dblect.manifest import (
    ConstraintSpec,
    ConstraintType,
    DbtTestMetadata,
    ModelConfig,
    Node,
    ResourceType,
)
from dblect.types import ModelContract
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source

_DUCKDB = profile_for_adapter("duckdb")


_LINE_COLS = _cols(order_id="INT", line_number="INT", amount="DECIMAL")

# A per-line leaf model: no FROM, so only its declared compound key grounds it.
_ORDER_LINES = _node(
    "model.shop.order_lines",
    sql="select 1 as order_id, 1 as line_number, 1.0 as amount",
    columns=_LINE_COLS,
)


def _declare_order_lines_key() -> None:
    class OrderLines(ModelContract):
        dbt_model = "order_lines"

        @contract
        def per_line(self: ContractSelf) -> object:
            return self.key(self.order_id, self.line_number)


def _grain_findings(report: CheckReport) -> list[CheckFinding]:
    return [f for f in report.findings if f.kind is CheckFindingKind.GRAIN_NOT_ESTABLISHED]


# --- the witnessed defeater fires -------------------------------------------------


def test_declared_grain_defeated_by_a_surviving_finer_key_is_a_finding() -> None:
    # fct_orders declares one row per order but selects from the per-line model
    # without collapsing. The consumer pins where the finding lands: at fct_orders,
    # not at the sum downstream.
    _declare_order_lines_key()

    class FctOrders(ModelContract):
        dbt_model = "fct_orders"

        @contract
        def one_row_per_order(self: ContractSelf) -> object:
            return self.grain(per=self.order_id)

    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, line_number, amount from order_lines",
        columns=_LINE_COLS,
    )
    consumer = _node(
        "model.shop.daily",
        sql="select order_id, sum(amount) as total from fct_orders group by order_id",
        columns=_cols(order_id="INT", total="DECIMAL"),
    )
    report = run_check(_manifest(_ORDER_LINES, fct, consumer), _DUCKDB)

    findings = _grain_findings(report)
    assert [f.model_unique_id for f in findings] == ["model.shop.fct_orders"]
    message = findings[0].message
    assert "not established" in message
    assert "order_id" in message
    assert "line_number" in message
    # The data may still satisfy the grain, so the finding never claims a violation.
    assert "violat" not in message.lower()


# --- the grain is established: silence ---------------------------------------------


def test_a_collapse_to_the_declared_grain_is_established_and_silent() -> None:
    # The GROUP BY re-derives the declared key.
    _declare_order_lines_key()

    class FctOrders(ModelContract):
        dbt_model = "fct_orders"

        @contract
        def one_row_per_order(self: ContractSelf) -> object:
            return self.grain(per=self.order_id)

    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, sum(amount) as amount from order_lines group by order_id",
        columns=_cols(order_id="INT", amount="DECIMAL"),
    )
    report = run_check(_manifest(_ORDER_LINES, fct), _DUCKDB)
    assert _grain_findings(report) == []


def test_coverage_runs_through_the_fd_closure() -> None:
    # The derived key is (order_id, region); the declared ``order_id -> region``
    # closes it to (order_id).
    class OrderRegions(ModelContract):
        dbt_model = "order_regions"

        @contract
        def per_order_region(self: ContractSelf) -> object:
            return self.key(self.order_id, self.region)

        @contract
        def order_pins_region(self: ContractSelf) -> object:
            return self.order_id.determines(self.region)

    class FctOrders(ModelContract):
        dbt_model = "fct_orders"

        @contract
        def one_row_per_order(self: ContractSelf) -> object:
            return self.grain(per=self.order_id)

    regions = _node(
        "model.shop.order_regions",
        sql="select 1 as order_id, 'emea' as region, 1.0 as amount",
        columns=_cols(order_id="INT", region="TEXT", amount="DECIMAL"),
    )
    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, region, amount from order_regions",
        columns=_cols(order_id="INT", region="TEXT", amount="DECIMAL"),
    )
    report = run_check(_manifest(regions, fct), _DUCKDB)
    assert _grain_findings(report) == []


# --- no witness: the walk's silence is not evidence ---------------------------------


def test_absence_of_any_inferred_key_is_not_a_witness() -> None:
    # No upstream key, so nothing is derived for fct_orders: neither established
    # nor defeated. Firing here would flag every model whose upstream declares nothing.
    class FctOrders(ModelContract):
        dbt_model = "fct_orders"

        @contract
        def one_row_per_order(self: ContractSelf) -> object:
            return self.grain(per=self.order_id)

    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, line_number, amount from order_lines",
        columns=_LINE_COLS,
    )
    report = run_check(_manifest(_ORDER_LINES, fct), _DUCKDB)
    assert _grain_findings(report) == []


def test_a_declared_key_on_a_source_has_no_construction_to_judge() -> None:
    # A source has no SQL, so it is absent from the derived record. A ``unique`` test
    # on a source is ordinary, so this shape must not crash the run.
    orders = _source("source.shop.raw.orders", columns=_cols(order_id="INT", amount="DECIMAL"))
    unique_test = _node(
        "test.shop.unique_orders_order_id",
        kind=ResourceType.OTHER,
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": "order_id"}),
        attached_node="source.shop.raw.orders",
    )
    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, amount from orders",
        columns=_cols(order_id="INT", amount="DECIMAL"),
    )
    report = run_check(_manifest(orders, unique_test, fct), _DUCKDB)
    assert _grain_findings(report) == []


# --- a collapse the walk cannot yet see: expected failure --------------------------


@pytest.mark.xfail(strict=True, reason="the emitter needs the walk's exactness record")
def test_correlated_subquery_collapse_is_not_yet_recognized() -> None:
    # The correlated subquery collapses to one row per order, but the derivation is
    # not exact for this shape, so the finer key still appears to survive.
    _declare_order_lines_key()

    class TopLine(ModelContract):
        dbt_model = "top_line"

        @contract
        def one_row_per_order(self: ContractSelf) -> object:
            return self.grain(per=self.order_id)

    top_line = _node(
        "model.shop.top_line",
        sql=(
            "select order_id, line_number, amount from order_lines "
            "where line_number = (select max(x.line_number) from order_lines x "
            "where x.order_id = order_lines.order_id)"
        ),
        columns=_LINE_COLS,
    )
    report = run_check(_manifest(_ORDER_LINES, top_line), _DUCKDB)
    assert _grain_findings(report) == []


# --- declaration channels: every case of the closed provenance type decided ---------
#
# The contract channel fires in the first test; this table covers the rest. A dbt
# ``unique`` test is judged like a contract grain. A ``where``-filtered test is a claim
# over a row filter, owned by activation. A native constraint is the warehouse's to
# enforce (#48). A deduplicating incremental's ``unique_key`` is enforced on write (#7),
# and so is any other key declared on that model.


def _unique_test_node(*, where: str | None) -> Node:
    return _node(
        "test.shop.unique_fct_orders_order_id",
        kind=ResourceType.OTHER,
        test_metadata=DbtTestMetadata(
            name="unique", kwargs={"column_name": "order_id"}, where=where
        ),
        attached_node="model.shop.fct_orders",
    )


@dataclass(frozen=True)
class _Channel:
    """One surface a key on ``order_id`` can arrive through, as the manifest carries it."""

    label: str
    fires: bool
    test_node: Node | None = None
    config: ModelConfig | None = None
    constraints: tuple[ConstraintSpec, ...] = ()


_CHANNELS = (
    _Channel("dbt_unique_test", True, test_node=_unique_test_node(where=None)),
    _Channel("conditional_unique_test", False, test_node=_unique_test_node(where="order_id > 0")),
    _Channel(
        "native_constraint",
        False,
        constraints=(ConstraintSpec(type=ConstraintType.UNIQUE, columns=("order_id",)),),
    ),
    _Channel(
        "incremental_unique_key",
        False,
        config=ModelConfig(
            materialized="incremental", incremental_strategy="merge", unique_key=("order_id",)
        ),
    ),
    _Channel(
        "incremental_unique_key_with_unique_test",
        False,
        test_node=_unique_test_node(where=None),
        config=ModelConfig(
            materialized="incremental", incremental_strategy="merge", unique_key=("order_id",)
        ),
    ),
)


@pytest.mark.parametrize("channel", _CHANNELS, ids=lambda c: c.label)
def test_declaration_channels_decide_what_is_judged(channel: _Channel) -> None:
    _declare_order_lines_key()
    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, line_number, amount from order_lines",
        columns=_LINE_COLS,
        config=channel.config,
        constraints=channel.constraints,
    )
    extra = (channel.test_node,) if channel.test_node is not None else ()
    report = run_check(_manifest(_ORDER_LINES, fct, *extra), _DUCKDB)

    findings = _grain_findings(report)
    expected = ["model.shop.fct_orders"] if channel.fires else []
    assert [f.model_unique_id for f in findings] == expected
