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

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFinding, CheckFindingKind, CheckReport, run_check
from dblect.contracts import ContractSelf, contract
from dblect.manifest import DbtTestMetadata, Manifest, ModelConfig, Node, ResourceType
from dblect.manifest.parse import Column
from dblect.types import ModelContract

_DUCKDB = profile_for_adapter("duckdb")


def _cols(**types: str) -> Mapping[str, Column]:
    return {n: Column(name=n, data_type=t, description=None) for n, t in types.items()}


def _node(
    uid: str,
    *,
    sql: str | None,
    columns: Mapping[str, Column],
    config: ModelConfig | None = None,
) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=tuple(uid.split(".")[1:]),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=f"models/{uid.split('.')[-1]}.sql",
        columns=columns,
        config=config,
    )


def _manifest(*nodes: Node) -> Manifest:
    return Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )


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
    # report nothing.
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
    report = run_check(_manifest(_ORDER_LINES, fct), _DUCKDB)

    findings = _grain_findings(report)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.model_unique_id == "model.shop.fct_orders"
    message = finding.message
    assert "not established" in message
    assert "order_id" in message
    assert "line_number" in message
    # Honesty of grade: the construction fails to establish the grain; the data may
    # still satisfy it, so the finding never claims a violation.
    assert "violat" not in message.lower()


def test_finding_lands_at_the_declaring_model_not_the_downstream_consumer() -> None:
    # The point of the emitter (issue #202): raise the finding at the model whose
    # grain is unestablished, not at the downstream sum that eventually trips.
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

    assert [f.model_unique_id for f in _grain_findings(report)] == ["model.shop.fct_orders"]


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


# --- provenance channels: every case of the closed type decided ---------------------


def test_a_dbt_unique_test_declaration_is_judged_like_a_contract_grain() -> None:
    # The same claim authored through the dbt ``unique`` test channel: provenance
    # carries no authority ordering, so a declared key is a declared key whichever
    # surface stated it. The drift shape that fires for a contract grain fires here.
    _declare_order_lines_key()

    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, line_number, amount from order_lines",
        columns=_LINE_COLS,
    )
    unique_test = Node(
        unique_id="test.shop.unique_fct_orders_order_id",
        name="unique_fct_orders_order_id",
        resource_type=ResourceType.OTHER,
        fqn=("shop", "unique_fct_orders_order_id"),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": "order_id"}),
        attached_node="model.shop.fct_orders",
    )
    report = run_check(_manifest(_ORDER_LINES, fct, unique_test), _DUCKDB)

    findings = _grain_findings(report)
    assert [f.model_unique_id for f in findings] == ["model.shop.fct_orders"]


def test_an_incremental_unique_key_is_discharged_by_the_write_not_the_select() -> None:
    # A deduplicating incremental materialization enforces its ``unique_key`` on
    # write: the SELECT is expected to carry finer rows and the merge collapses
    # them. That key is a CompileValue fact, not a declaration about the SELECT's
    # own output, so the emitter must not judge it (the incremental-grain stream,
    # issue #7, owns that write-path reasoning).
    _declare_order_lines_key()

    fct = _node(
        "model.shop.fct_orders",
        sql="select order_id, line_number, amount from order_lines",
        columns=_LINE_COLS,
        config=ModelConfig(
            materialized="incremental",
            incremental_strategy="merge",
            unique_key=("order_id",),
        ),
    )
    report = run_check(_manifest(_ORDER_LINES, fct), _DUCKDB)
    assert _grain_findings(report) == []
