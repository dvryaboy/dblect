# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance; annotating it that way trips pyright's self-supertype rule while keeping
# the proxy usage checked. Typed ``self`` in authored contracts is the stubs concern.
"""A declared ``zip determines city`` through the real check, from contract to
finding. The witness is a UNION whose every arm carries the dependency: each arm can
honour it alone while the two disagree on some zip, so the merge is the one operator
we can point at, and the finding says not established, never violated."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFinding, CheckFindingKind, run_check
from dblect.contracts import ContractSelf, contract
from dblect.manifest import Column, Node
from dblect.types import ModelContract
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_DUCKDB = profile_for_adapter("duckdb")
_ADDRESS_COLS = _cols(zip="TEXT", city="TEXT")
_AT_DIM = ["model.shop.dim_addresses"]

# Leaf staging models: no FROM, so the walk infers nothing and only a declaration
# can give them a dependency.
_STG_US = _node(
    "model.shop.stg_us_addresses",
    sql="select '1' as zip, 'austin' as city",
    columns=_ADDRESS_COLS,
)
_STG_INTL = _node(
    "model.shop.stg_intl_addresses",
    sql="select '1' as zip, 'oslo' as city",
    columns=_ADDRESS_COLS,
)

_UNIONED_CTE = (
    "with unioned as (\n"
    "  select zip, city from stg_us_addresses\n"
    "  union all\n"
    "  select zip, city from stg_intl_addresses\n"
    ")\n"
)


def _declare_zip_determines_city(model: str) -> None:
    class Declares(ModelContract):
        dbt_model = model

        @contract
        def zip_determines_city(self: ContractSelf) -> object:
            return self.zip.determines(self.city)


def _declare_all(*models: str) -> None:
    for model in models:
        _declare_zip_determines_city(model)


def _dim(sql: str, columns: Mapping[str, Column] = _ADDRESS_COLS) -> Node:
    return _node("model.shop.dim_addresses", sql=sql, columns=columns)


def _findings(*nodes: Node) -> list[CheckFinding]:
    report = run_check(_manifest(*nodes), _DUCKDB)
    return [f for f in report.findings if f.kind is CheckFindingKind.DEPENDENCY_NOT_ESTABLISHED]


def test_the_finding_lands_at_the_model_and_points_at_the_union() -> None:
    _declare_all("stg_us_addresses", "stg_intl_addresses", "dim_addresses")
    dim = _dim(
        "select zip, city from stg_us_addresses\nunion all\nselect zip, city from stg_intl_addresses"
    )
    findings = _findings(_STG_US, _STG_INTL, dim)

    assert [f.model_unique_id for f in findings] == _AT_DIM
    finding = findings[0]
    assert "not established" in finding.message
    assert "zip" in finding.message
    assert "city" in finding.message
    assert "union" in finding.message.lower()
    # The data may still satisfy the dependency, so no claim of violation.
    assert "violat" not in finding.message.lower()
    assert (finding.line_start, finding.line_end) == (1, 3)
    assert finding.column == "city"


@pytest.mark.parametrize(
    "dim_sql",
    [
        pytest.param(_UNIONED_CTE + "select * from unioned", id="passthrough_cte"),
        pytest.param(
            "select zip, city from stg_us_addresses "
            "union all select zip, city from stg_intl_addresses "
            "union all select zip, city from stg_us_addresses",
            id="three_arms_one_witness",
        ),
    ],
)
def test_the_witness_reaches_the_output_through_common_shapes(dim_sql: str) -> None:
    _declare_all("stg_us_addresses", "stg_intl_addresses", "dim_addresses")
    findings = _findings(_STG_US, _STG_INTL, _dim(dim_sql))
    assert [f.model_unique_id for f in findings] == _AT_DIM


def test_the_witness_follows_a_rename_after_the_union() -> None:
    # The declaration is in the model's output names, not the arms'.
    _declare_all("stg_us_addresses", "stg_intl_addresses")

    class DimAddresses(ModelContract):
        dbt_model = "dim_addresses"

        @contract
        def postal_code_determines_city(self: ContractSelf) -> object:
            return self.postal_code.determines(self.city)

    dim = _dim(
        _UNIONED_CTE + "select zip as postal_code, city from unioned",
        columns=_cols(postal_code="TEXT", city="TEXT"),
    )
    assert [f.model_unique_id for f in _findings(_STG_US, _STG_INTL, dim)] == _AT_DIM


def test_arms_align_by_position_not_by_name() -> None:
    # SQL names a union after its first arm; the second arm spells the columns
    # differently and still carries the dependency into the union.
    _declare_all("stg_us_addresses", "dim_addresses")

    class Intl(ModelContract):
        dbt_model = "stg_intl_addresses"

        @contract
        def postal_determines_town(self: ContractSelf) -> object:
            return self.postal.determines(self.town)

    intl = _node(
        "model.shop.stg_intl_addresses",
        sql="select '1' as postal, 'oslo' as town",
        columns=_cols(postal="TEXT", town="TEXT"),
    )
    dim = _dim(
        "select zip, city from stg_us_addresses union all select postal, town from stg_intl_addresses"
    )
    assert [f.model_unique_id for f in _findings(_STG_US, intl, dim)] == _AT_DIM


def test_an_arm_carries_the_dependency_through_a_chain() -> None:
    # The US arm states zip -> region and region -> city, never zip -> city: an arm
    # is judged by what it entails. (Both arms keep region in the projection; the
    # walk carries spelled-out dependencies, so dropping the middle drops the chain.)
    class Us(ModelContract):
        dbt_model = "stg_us_addresses"

        @contract
        def zip_determines_region(self: ContractSelf) -> object:
            return self.zip.determines(self.region)

        @contract
        def region_determines_city(self: ContractSelf) -> object:
            return self.region.determines(self.city)

    _declare_all("stg_intl_addresses", "dim_addresses")
    regional = _cols(zip="TEXT", region="TEXT", city="TEXT")
    us = _node(
        "model.shop.stg_us_addresses",
        sql="select '1' as zip, 'tx' as region, 'austin' as city",
        columns=regional,
    )
    intl = _node(
        "model.shop.stg_intl_addresses",
        sql="select '1' as zip, 'no' as region, 'oslo' as city",
        columns=regional,
    )
    dim = _dim(
        "select zip, region, city from stg_us_addresses "
        "union all select zip, region, city from stg_intl_addresses",
        columns=regional,
    )
    assert [f.model_unique_id for f in _findings(us, intl, dim)] == _AT_DIM


def test_a_single_carrying_source_establishes_it() -> None:
    _declare_all("stg_us_addresses", "dim_addresses")
    assert _findings(_STG_US, _dim("select zip, city from stg_us_addresses")) == []


def test_a_group_by_on_the_determinant_re_establishes_it() -> None:
    # Both arms carry the dependency, so the union is a witness; the GROUP BY
    # collapses to one row per zip, and established wins over witnessed.
    _declare_all("stg_us_addresses", "stg_intl_addresses", "dim_addresses")
    dim = _dim(_UNIONED_CTE + "select zip, max(city) as city from unioned group by zip")
    assert _findings(_STG_US, _STG_INTL, dim) == []


def test_an_arm_that_carries_nothing_is_not_a_witness() -> None:
    # The intl arm declares nothing: the dependency may have broken earlier, so
    # the union cannot be named as the operator that broke it.
    _declare_all("stg_us_addresses", "dim_addresses")
    dim = _dim(
        "select zip, city from stg_us_addresses union all select zip, city from stg_intl_addresses"
    )
    assert _findings(_STG_US, _STG_INTL, dim) == []


def test_a_declaration_with_no_dependency_anywhere_is_not_a_witness() -> None:
    _declare_zip_determines_city("dim_addresses")
    assert _findings(_STG_US, _dim("select zip, city from stg_us_addresses")) == []


# A UNION, distinct or not, merges rows from both arms, so the arms can disagree.
# INTERSECT and EXCEPT keep a subset of the left arm's rows, and a dependency that
# holds on a relation holds on any subset of it; they are no witness.
@pytest.mark.parametrize(
    ("operator", "fires"),
    [("union all", True), ("union", True), ("intersect", False), ("except", False)],
)
def test_set_operations_decide_whether_a_merge_is_a_witness(operator: str, fires: bool) -> None:
    _declare_all("stg_us_addresses", "stg_intl_addresses", "dim_addresses")
    dim = _dim(
        f"select zip, city from stg_us_addresses {operator} "
        "select zip, city from stg_intl_addresses"
    )
    findings = _findings(_STG_US, _STG_INTL, dim)
    assert [f.model_unique_id for f in findings] == (_AT_DIM if fires else [])
