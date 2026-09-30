# pyright: reportUnusedClass=false
"""Dead predicate over a closed value set: end-to-end through ``run_check``.

Each row is a SQL fixture over one shared contract, run through the shared
check-table runner: the SQL is what keeps this from being tautological, since
it exercises the AST reader and the boolean-context walk, while
``test_pbt_dead_predicate_soundness.py`` covers the semantics against
materialized data. The rows enumerate the closed spaces the design fixes:
comparison form x polarity (dead vs. redundant), the literal classes
(member, stray, case-only, kind mismatch), the boolean contexts a comparison
can sit in, and the CASE-coverage shapes (indicator idioms exempted, a
default that is not absent/NULL/literal left undecidable).
"""

from __future__ import annotations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFindingKind, run_check
from dblect.manifest import DbtTestMetadata, Manifest, ResourceType
from dblect.types import ModelContract, NominalEnum
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests.check._check_table import CheckCase, run_check_case

_DUCKDB = profile_for_adapter("duckdb")

_DEAD = CheckFindingKind.DEAD_PREDICATE
_CASE_ONLY = CheckFindingKind.DEAD_PREDICATE_CASE_ONLY
_REDUNDANT = CheckFindingKind.REDUNDANT_PREDICATE
_COVERAGE = CheckFindingKind.CASE_LEAVES_ENUM_MEMBER_UNHANDLED


class Status(NominalEnum):
    SHIPPED = "shipped"
    PENDING = "pending"
    CANCELLED = "cancelled"


def _orders_manifest(sql: str) -> Manifest:
    class Orders(ModelContract):
        dbt_model = "orders"
        status: Status

    orders = _node("model.shop.orders", sql, columns=_cols(status="VARCHAR", k="INT"))
    return _manifest(orders)


# --- comparison form x literal class x boolean context x polarity ------------

_ATOM_CASES: tuple[CheckCase, ...] = (
    CheckCase(
        "eq_non_member_is_dead",
        "select * from orders where status = 'shipd'",
        expected=(_DEAD,),
        wording=("always empty",),
    ),
    CheckCase("eq_member_is_silent", "select * from orders where status = 'shipped'"),
    CheckCase("eq_kind_mismatch_is_silent", "select * from orders where status = 1"),
    CheckCase(
        "eq_case_only_fires_the_case_only_kind",
        "select * from orders where status = 'Shipped'",
        expected=(_CASE_ONLY,),
        wording=("only by case",),
    ),
    CheckCase(
        "null_safe_eq_non_member_is_constant_false",
        "select * from orders where status is not distinct from 'shipd'",
        expected=(_DEAD,),
        wording=("constant FALSE",),
    ),
    CheckCase(
        "neq_non_member_is_redundant",
        "select * from orders where status != 'shipd'",
        expected=(_REDUNDANT,),
        wording=("NULL rows",),
    ),
    CheckCase(
        "null_safe_neq_non_member_is_constant_true",
        "select * from orders where status is distinct from 'shipd'",
        expected=(_REDUNDANT,),
        wording=("constant TRUE",),
    ),
    CheckCase(
        "in_non_member_is_dead",
        "select * from orders where status in ('shipd')",
        expected=(_DEAD,),
    ),
    CheckCase(
        "in_reports_only_the_stray_member_not_the_whole_list",
        "select * from orders where status in ('shipped', 'shipd')",
        expected=(_DEAD,),
    ),
    CheckCase(
        "not_in_sugar_with_a_null_member_is_silent",
        "select * from orders where status not in ('shipd', null)",
    ),
    CheckCase(
        "parenthesized_not_in_with_a_null_member_is_also_silent",
        # NOT (col IN (...)) parses with a Paren between the Not and the In,
        # unlike the NOT IN sugar; the NULL-member rule must recognise both.
        "select * from orders where not (status in ('shipd', null))",
    ),
    CheckCase(
        "not_in_without_a_null_member_fires_redundant",
        "select * from orders where status not in ('shipd')",
        expected=(_REDUNDANT,),
    ),
    CheckCase(
        "not_wrapper_flips_dead_to_redundant",
        "select * from orders where not (status = 'shipd')",
        expected=(_REDUNDANT,),
    ),
    CheckCase(
        "not_wrapper_flips_redundant_to_dead",
        "select * from orders where not (status != 'shipd')",
        expected=(_DEAD,),
    ),
    CheckCase(
        "join_on_non_member_never_matches",
        "select * from orders a join orders b on a.status = 'shipd'",
        expected=(_DEAD,),
        wording=("never matches",),
    ),
    CheckCase(
        "or_arm_non_member_is_this_disjunct_never_matches",
        "select * from orders where status = 'shipd' or k = 1",
        expected=(_DEAD,),
        wording=("disjunct",),
    ),
    CheckCase(
        "case_when_non_member_is_this_arm_never_taken",
        "select case when status = 'shipd' then 1 end as r from orders",
        expected=(_DEAD,),
        wording=("never taken",),
    ),
    CheckCase(
        "projected_dead_is_worded_never_true",
        "select status = 'shipd' as r from orders",
        expected=(_DEAD,),
        wording=("never true",),
    ),
    CheckCase(
        "projected_redundant_is_silent",
        # REDUNDANT_PREDICATE describes a filter; a bare projected boolean
        # filters nothing, so it never fires here.
        "select status != 'shipd' as r from orders",
    ),
    CheckCase(
        "column_to_column_equality_is_silent",
        "select * from orders a join orders b on a.status = b.status",
    ),
    CheckCase(
        "ordering_like_and_is_are_silent",
        "select * from orders where status > 'a' and status like 'a%' and status is null",
    ),
    CheckCase(
        "null_safe_eq_with_a_null_literal_operand_is_silent",
        "select * from orders where status is not distinct from null",
    ),
    CheckCase(
        "dead_predicate_survives_through_a_cte",
        "with c as (select status from orders) select * from c where status = 'shipd'",
        expected=(_DEAD,),
    ),
)


@pytest.mark.parametrize("case", _ATOM_CASES, ids=lambda c: c.id)
def test_dead_predicate_atoms(case: CheckCase) -> None:
    run_check_case(case, _orders_manifest(case.sql), _DUCKDB)


def test_dead_predicate_finding_carries_the_compared_column() -> None:
    """The compared column rides on the finding, for the report and for
    ``-- noqa`` line/column matching, the same as ``_join_key_findings``
    carries it for domain types."""
    report = run_check(_orders_manifest("select * from orders where status = 'shipd'"), _DUCKDB)
    (dead,) = [f for f in report.findings if f.kind is _DEAD]
    assert dead.column == "status"


# --- CASE coverage ------------------------------------------------------------

_CASE_COVERAGE_CASES: tuple[CheckCase, ...] = (
    CheckCase(
        "searched_case_missing_a_member_with_else_other_fires_coverage",
        "select case when status = 'shipped' then 'S' when status = 'pending' then 'P' "
        "else 'other' end as r from orders",
        expected=(_COVERAGE,),
        wording=("cancelled",),
    ),
    CheckCase(
        "simple_case_missing_a_member_fires_coverage",
        "select case status when 'shipped' then 'S' when 'pending' then 'P' end as r from orders",
        expected=(_COVERAGE,),
    ),
    CheckCase(
        "case_with_else_null_still_fires_coverage",
        "select case when status = 'shipped' then 'S' when status = 'pending' then 'P' "
        "else null end as r from orders",
        expected=(_COVERAGE,),
    ),
    CheckCase(
        "case_with_a_passthrough_else_is_silent",
        # ELSE status passes an unmatched member through unchanged, so nothing
        # is "routed to the default"; only an absent, NULL, or literal default
        # is a place a member can fall into.
        "select case when status = 'shipped' then 'S' when status = 'pending' then 'P' "
        "else status end as r from orders",
    ),
    CheckCase(
        "case_with_an_unrelated_column_else_is_silent",
        "select case when status = 'shipped' then 'S' when status = 'pending' then 'P' "
        "else cast(k as varchar) end as r from orders",
    ),
    CheckCase(
        "case_covering_every_member_is_silent",
        "select case when status = 'shipped' then 'S' when status = 'pending' then 'P' "
        "when status = 'cancelled' then 'C' end as r from orders",
    ),
    CheckCase(
        "one_arm_indicator_is_silent",
        "select sum(case when status = 'cancelled' then 1 else 0 end) as n from orders",
    ),
    CheckCase(
        "multi_arm_same_then_indicator_is_silent",
        # The indicator idiom generalizes past one arm: a disjunction (every
        # THEN the same literal) still computes a flag, not a remap.
        "select sum(case when status = 'shipped' then 1 when status = 'pending' then 1 "
        "else 0 end) as n from orders",
    ),
    CheckCase(
        "multi_arm_distinct_then_fires_even_outside_the_domain",
        # Lying outside the column's own domain is not, by itself, the
        # indicator shape: a coded remap that routes a missing member to its
        # default is exactly the shape this finding exists for.
        "select case when status = 'shipped' then 1 when status = 'pending' then 2 "
        "else 0 end as r from orders",
        expected=(_COVERAGE,),
    ),
    CheckCase(
        "a_non_column_arm_makes_coverage_undecidable",
        "select case when status = 'shipped' then 'S' when k > 1 then 'K' end as r from orders",
    ),
)


@pytest.mark.parametrize("case", _CASE_COVERAGE_CASES, ids=lambda c: c.id)
def test_case_coverage(case: CheckCase) -> None:
    run_check_case(case, _orders_manifest(case.sql), _DUCKDB)


# --- lineage: a different manifest shape per cluster, not just varying SQL ----


class Extra(NominalEnum):
    """A second, unrelated enum for the union-of-two-sources tests: kept at
    module level since ``ModelContract``'s annotation resolution cannot see a
    function's local scope."""

    ARCHIVED = "archived"


def _passthrough_manifest(downstream_sql: str) -> Manifest:
    class RawOrders(ModelContract):
        dbt_model = "raw_orders"
        status: Status

    src = _node(
        "source.shop.raw.raw_orders", kind=ResourceType.SOURCE, columns=_cols(status="VARCHAR")
    )
    stg = _node(
        "model.shop.stg_orders", "select status from raw_orders", columns=_cols(status="VARCHAR")
    )
    downstream = _node("model.shop.downstream", downstream_sql, columns=_cols(status="VARCHAR"))
    return _manifest(src, stg, downstream)


_PASSTHROUGH_CASES: tuple[CheckCase, ...] = (
    CheckCase(
        "dead_predicate_survives_a_passthrough_model",
        "select * from stg_orders where status = 'shipd'",
        expected=(_DEAD,),
    ),
    CheckCase(
        "upper_silences_the_check_through_lineage",
        "select * from stg_orders where upper(status) = 'shipd'",
    ),
)


@pytest.mark.parametrize("case", _PASSTHROUGH_CASES, ids=lambda c: c.id)
def test_dead_predicate_through_lineage(case: CheckCase) -> None:
    run_check_case(case, _passthrough_manifest(case.sql), _DUCKDB)


def _union_manifest(downstream_sql: str) -> Manifest:
    class Src1(ModelContract):
        dbt_model = "s1"
        status: Status

    class Src2(ModelContract):
        dbt_model = "s2"
        status: Extra

    src1 = _node("source.shop.raw.s1", kind=ResourceType.SOURCE, columns=_cols(status="VARCHAR"))
    src2 = _node("source.shop.raw.s2", kind=ResourceType.SOURCE, columns=_cols(status="VARCHAR"))
    combined = _node(
        "model.shop.combined",
        "select status from s1 union all select status from s2",
        columns=_cols(status="VARCHAR"),
    )
    downstream = _node("model.shop.downstream", downstream_sql, columns=_cols(status="VARCHAR"))
    return _manifest(src1, src2, combined, downstream)


_UNION_CASES: tuple[CheckCase, ...] = (
    CheckCase(
        "stray_absent_from_both_sources_fires_dead",
        "select * from combined where status = 'nonexistent'",
        expected=(_DEAD,),
    ),
    CheckCase(
        "stray_present_in_only_one_source_stays_quiet",
        "select * from combined where status = 'archived'",
    ),
)


@pytest.mark.parametrize("case", _UNION_CASES, ids=lambda c: c.id)
def test_union_of_two_enumd_sources(case: CheckCase) -> None:
    run_check_case(case, _union_manifest(case.sql), _DUCKDB)


# --- a tuva-shaped fixture -----------------------------------------------------


class EncounterType(NominalEnum):
    ACUTE_INPATIENT = "acute inpatient"
    OUTPATIENT = "outpatient"
    EMERGENCY = "emergency"


def _tuva_manifest(downstream_sql: str) -> Manifest:
    class Encounters(ModelContract):
        dbt_model = "encounters__combined_claim_line_crosswalk"
        encounter_type: EncounterType

    src = _node(
        "source.tuva.core.encounters__combined_claim_line_crosswalk",
        kind=ResourceType.SOURCE,
        columns=_cols(encounter_type="VARCHAR"),
    )
    downstream = _node(
        "model.tuva.downstream", downstream_sql, columns=_cols(encounter_type="VARCHAR")
    )
    return _manifest(src, downstream)


_TUVA_CASES: tuple[CheckCase, ...] = (
    CheckCase(
        "typo_fires_dead_predicate",
        "select * from encounters__combined_claim_line_crosswalk "
        "where encounter_type = 'acute inpatent'",
        expected=(_DEAD,),
    ),
    CheckCase(
        "case_mismatch_fires_case_only",
        "select * from encounters__combined_claim_line_crosswalk "
        "where encounter_type = 'Acute Inpatient'",
        expected=(_CASE_ONLY,),
    ),
    CheckCase(
        "a_fresh_vocabulary_remap_missing_a_member_fires_coverage",
        "select case when encounter_type = 'acute inpatient' then 'inpatient' "
        "when encounter_type = 'emergency' then 'ed' else 'other' end as bucket "
        "from encounters__combined_claim_line_crosswalk",
        expected=(_COVERAGE,),
    ),
    CheckCase(
        "the_indicator_idiom_stays_quiet",
        "select sum(case when encounter_type = 'acute inpatient' then 1 else 0 end) as n "
        "from encounters__combined_claim_line_crosswalk",
    ),
)


@pytest.mark.parametrize("case", _TUVA_CASES, ids=lambda c: c.id)
def test_tuva_shaped_fixture(case: CheckCase) -> None:
    run_check_case(case, _tuva_manifest(case.sql), _DUCKDB)


# --- disjoint declarations: a CONTRACT_ISSUE at grounding ----------------------


def test_disjoint_declarations_are_a_contract_issue_and_do_not_crash() -> None:
    class Orders(ModelContract):
        dbt_model = "orders"
        status: Status  # {shipped, pending, cancelled}

    orders = _node(
        "model.shop.orders",
        "select * from orders where status = 'archived'",
        columns=_cols(status="VARCHAR"),
    )
    accepted = _node(
        "test.shop.av",
        kind=ResourceType.OTHER,
        depends_on=frozenset({"model.shop.orders"}),
        test_metadata=DbtTestMetadata(
            name="accepted_values",
            kwargs={"column_name": "status", "values": ["archived"]},  # disjoint from Status
        ),
        attached_node="model.shop.orders",
    )
    report = run_check(_manifest(orders, accepted), _DUCKDB)
    kinds = [f.kind for f in report.findings]
    assert CheckFindingKind.CONTRACT_ISSUE in kinds
    conflict = next(
        f
        for f in report.findings
        if f.kind is CheckFindingKind.CONTRACT_ISSUE and f.column == "status"
    )
    assert conflict.code is not None
    assert conflict.code.value == "value_domain_conflict"
    # The conflicting column grounds to "nothing declared" rather than
    # raising, so an unrelated stray literal on it is silent, not a crash.
    assert _DEAD not in kinds
