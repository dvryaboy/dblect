# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance; annotating it that way trips pyright's self-supertype rule while keeping
# the proxy usage checked. Typed ``self`` in authored contracts is the stubs concern.
"""The declared-grain not-established emitter, through the ``run_check`` boundary.

These pin the wiring a declared grain travels: contract to fact to propagation to
finding. The decision procedure itself is pinned against brute force in
``test_pbt_grain_established.py``, and the verdict vocabulary the wording follows is
``docs/design/refutation-and-verdicts.md``.

The load-bearing regression is the pre-reconcile record. Uniqueness reconciles
declared and inferred keys by meet, so the flow value always contains the
declaration and a declaration reading it would check itself; the drift test fails
against any implementation that reads the flow value rather than the recorded
inferred one.
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

# A per-line leaf model: no FROM, so the relation walk infers nothing and its
# declared compound key grounds it.
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
    # without collapsing, so the finer key (order_id, line_number) survives to its
    # output: the witnessed defeater, and the case an emitter reading the flow value
    # would miss. The downstream consumer pins where the finding lands: at the model
    # whose grain is unestablished, not at the sum that eventually trips over it.
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
    # Honesty of grade: the construction fails to establish the grain; the data may
    # still satisfy it, so the finding never claims a violation.
    assert "violat" not in message.lower()


# --- the grain is established: silence ---------------------------------------------


def test_a_collapse_to_the_declared_grain_is_established_and_silent() -> None:
    # The same declaration over SQL that aggregates to the declared grain: the
    # GROUP BY re-derives the key, so the entailment holds and nothing fires.
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
    # The surviving key is (order_id, region), strictly finer than the declared
    # grain (order_id); but a declared ``order_id determines region`` closes the
    # gap: unique on (order_id, region) plus the dependency is unique on (order_id),
    # so the grain is established and nothing fires.
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
    # The upstream relation declares no key, so the walk infers nothing for
    # fct_orders: the declared grain is neither re-derived nor defeated. Firing here
    # would flag every model whose upstream declares nothing; the emitter must
    # require a witnessed defeater, not an absent proof.
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
    # A source carries no SQL of its own, so the walk derives nothing for it and it
    # is absent from the record entirely. There is no entailment to judge, and the
    # emitter has to pass over it rather than reach for a value never recorded. A
    # ``unique`` test on a source is ordinary in a dbt project, so this is the shape
    # that would crash a whole run.
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


# --- declaration channels: every case of the closed provenance type decided ---------
#
# The contract channel's fire case is the drift test above; the table here covers the
# rest. A dbt ``unique`` test is judged like a contract grain (provenance carries no
# authority ordering), and it is the only other channel that fires. A ``where``-filtered
# test claims uniqueness only over a row filter, so it is activation's business, not a
# grain claim about the whole output. A native constraint is discharged (or not) by the
# warehouse's write path; the advisory-unenforced case is the unenforced-constraint
# finding's (#48). A deduplicating incremental's ``unique_key`` is enforced by the merge
# on write, so the SELECT is expected to carry finer rows (the incremental-grain
# stream, #7).


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
