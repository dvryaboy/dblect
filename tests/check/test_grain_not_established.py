# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance; annotating it that way trips pyright's self-supertype rule while keeping
# the proxy usage checked. Typed ``self`` in authored contracts is the stubs concern.
"""The declared-grain not-established emitter, through the ``run_check`` boundary.

A declared grain is an assumption downstream consumers trust and a proof obligation
at the declaring model: does the model's SQL, plus the upstream contracts, re-derive
uniqueness on the declared columns? The verdict vocabulary is
``docs/design/refutation-and-verdicts.md``: the finding fires only on a witnessed
defeater (a strictly finer key surviving to the output with no collapse to the
declared grain), never on the key walk's own silence, and it reports "declared but
not established", never "violated" (every extensional claim holds vacuously on the
empty relation).

The load-bearing regression here is the pre-reconcile record: uniqueness reconciles
declared and inferred keys by meet, so the post-reconcile flow value always contains
the declaration and a declaration would check itself. The drift test fails against
any implementation that reads the flow value instead of the recorded inferred one.
"""

from __future__ import annotations

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
    # without collapsing, so the strictly finer key (order_id, line_number) survives
    # to its output: the witnessed defeater. This is also the pre-reconcile
    # regression: the flow value contains the declared grain (meet unions it in), so
    # an emitter reading the flow value would see the declaration satisfy itself and
    # report nothing. The downstream consumer pins where the finding lands (issue
    # #202): at the model whose grain is unestablished, not at the sum that
    # eventually trips over it.
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


def test_a_non_minimal_declared_grain_is_covered_and_silent() -> None:
    # The declared grain is coarser than the surviving key: unique on (order_id)
    # implies unique on (order_id, line_number), so a non-minimal grain must not
    # false-fire (the ``detect_join_fanout`` hardening, applied here).
    _declare_order_lines_key()

    class FctOrders(ModelContract):
        dbt_model = "fct_orders"

        @contract
        def one_row_per_order_and_line(self: ContractSelf) -> object:
            return self.grain(per=(self.order_id, self.line_number))

    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, line_number, sum(amount) as amount from order_lines "
        "group by order_id, line_number",
        columns=_LINE_COLS,
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


def test_a_leaf_declaration_has_no_entailment_to_judge() -> None:
    # A grain declared on a model with no upstream derivation to walk (a leaf
    # SELECT with no FROM) grounds a trusted fact; there is no construction to
    # re-derive it from, so nothing fires.
    class OrderLines(ModelContract):
        dbt_model = "order_lines"

        @contract
        def per_line(self: ContractSelf) -> object:
            return self.grain(per=(self.order_id, self.line_number))

    report = run_check(_manifest(_ORDER_LINES), _DUCKDB)
    assert _grain_findings(report) == []


def test_an_unrelated_surviving_key_is_not_a_witness() -> None:
    # The surviving key (line_id) is not strictly finer than the declared grain
    # (order_id): it does not contain it, so it says nothing about rows per order
    # (every order may have exactly one line). No witness, no finding.
    class OrderLines(ModelContract):
        dbt_model = "order_lines"

        @contract
        def per_line(self: ContractSelf) -> object:
            return self.key(self.line_id)

    class FctOrders(ModelContract):
        dbt_model = "fct_orders"

        @contract
        def one_row_per_order(self: ContractSelf) -> object:
            return self.grain(per=self.order_id)

    lines = _node(
        "model.shop.order_lines",
        sql="select 1 as line_id, 1 as order_id, 1.0 as amount",
        columns=_cols(line_id="INT", order_id="INT", amount="DECIMAL"),
    )
    fct = _node(
        "model.shop.fct_orders",
        sql="select line_id, order_id, amount from order_lines",
        columns=_cols(line_id="INT", order_id="INT", amount="DECIMAL"),
    )
    report = run_check(_manifest(lines, fct), _DUCKDB)
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


@pytest.mark.parametrize(
    ("channel", "fires"),
    [
        ("dbt_unique_test", True),
        ("conditional_unique_test", False),
        ("native_constraint", False),
        ("incremental_unique_key", False),
    ],
)
def test_declaration_channels_decide_what_is_judged(channel: str, fires: bool) -> None:
    _declare_order_lines_key()

    extra_nodes: list[Node] = []
    config: ModelConfig | None = None
    constraints: tuple[ConstraintSpec, ...] = ()
    if channel == "dbt_unique_test":
        extra_nodes.append(_unique_test_node(where=None))
    elif channel == "conditional_unique_test":
        extra_nodes.append(_unique_test_node(where="order_id > 0"))
    elif channel == "native_constraint":
        constraints = (ConstraintSpec(type=ConstraintType.UNIQUE, columns=("order_id",)),)
    elif channel == "incremental_unique_key":
        config = ModelConfig(
            materialized="incremental",
            incremental_strategy="merge",
            unique_key=("order_id",),
        )

    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, line_number, amount from order_lines",
        columns=_LINE_COLS,
        config=config,
        constraints=constraints,
    )
    report = run_check(_manifest(_ORDER_LINES, fct, *extra_nodes), _DUCKDB)

    findings = _grain_findings(report)
    assert [f.model_unique_id for f in findings] == (["model.shop.fct_orders"] if fires else [])
