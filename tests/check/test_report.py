"""Rendering a ``CheckReport`` to text and JSON.

The text format is for terminals (a finding per model, click-through path); the
JSON format is a versioned, stable schema for CI and editors. These pin the shape
both consumers depend on.
"""

from __future__ import annotations

import json

from dblect.check import (
    CheckFinding,
    CheckFindingKind,
    CheckReport,
    UnbuiltModel,
    render_json,
    render_text,
)
from dblect.check.coverage import (
    GroundingCoverage,
    PropertyGrounding,
    ResolutionCoverage,
    WorldCoverage,
)
from dblect.loader import LoadIssue


def _report(
    *findings: CheckFinding,
    load_issues: tuple[LoadIssue, ...] = (),
    unbuilt: tuple[UnbuiltModel, ...] = (),
    resolution: ResolutionCoverage | None = None,
    grounding: GroundingCoverage | None = None,
    worlds: WorldCoverage | None = None,
) -> CheckReport:
    return CheckReport(
        findings=findings,
        load_issues=load_issues,
        unbuilt=unbuilt,
        contracts_resolved=2,
        models_propagated=3,
        predicates_collected=1,
        resolution=resolution or ResolutionCoverage(0, 0, 0, ()),
        grounding=grounding or GroundingCoverage((), 0, 0),
        worlds=worlds or WorldCoverage(worlds_enumerated=1, axes_enumerated=()),
    )


def test_text_lists_each_finding_with_location() -> None:
    finding = CheckFinding(
        kind=CheckFindingKind.AGGREGATION_NOT_WELL_TYPED,
        message="the sum is not well typed",
        model_unique_id="model.shop.revenue_by_country",
        file_path="models/revenue_by_country.sql",
        column="total",
    )
    text = render_text(_report(finding))
    assert "checked 2 contracts over 3 models: 1 finding" in text
    assert "aggregation_not_well_typed  model.shop.revenue_by_country.total" in text
    assert "models/revenue_by_country.sql" in text


def test_clean_report_reads_as_zero_findings() -> None:
    assert "0 findings" in render_text(_report())


def test_json_is_a_stable_versioned_schema() -> None:
    finding = CheckFinding(
        kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
        message="contradicted",
        model_unique_id="model.shop.orders",
        column="amount",
    )
    payload = json.loads(render_json(_report(finding, load_issues=(LoadIssue("m", "boom"),))))
    assert payload["schema_version"] == "3"
    assert payload["summary"] == {
        "contracts_resolved": 2,
        "models_propagated": 3,
        "models_analyzed": 3,
        "predicates_collected": 1,
        "findings": 1,
        "load_issues": 1,
        "unbuilt": 0,
    }
    assert payload["findings"][0]["kind"] == "domain_type_contradiction"
    assert payload["findings"][0]["column"] == "amount"
    assert payload["load_issues"][0]["module"] == "m"


def test_coverage_is_rendered_in_both_formats() -> None:
    resolution = ResolutionCoverage(
        resolved_columns=8, blind_columns=2, unexpanded_stars=1, worst_models=()
    )
    grounding = GroundingCoverage(
        by_property=(PropertyGrounding("domain_type", grounded=3, resolved=10),),
        contract_columns=4,
        contract_columns_checkable=3,
    )
    report = _report(resolution=resolution, grounding=grounding)

    # 8 resolved of 11 sites (8 resolved columns + 2 blind columns + 1 unexpanded star).
    text = render_text(report)
    assert "resolution: 72.7% of columns (8/11)" in text
    assert "unexpanded SELECT *" in text
    assert "domain_type 3/10" in text
    assert "contract columns checkable: 3/4" in text

    cov = json.loads(render_json(report))["coverage"]
    assert cov["resolution"] == {
        "resolved_columns": 8,
        "blind_columns": 2,
        "sites": 11,
        "unexpanded_stars": 1,
        "fraction": 8 / 11,
    }
    assert cov["grounding"]["by_property"][0] == {
        "property": "domain_type",
        "grounded": 3,
        "resolved": 10,
    }
    assert cov["grounding"]["contract_columns_checkable"] == 3


def test_world_coverage_reads_as_one_base_world_by_default() -> None:
    # The single-world run states its one-world scope plainly, so a clean report is
    # not mistaken for one that covered every configuration.
    text = render_text(_report())
    assert "worlds: 1 (base); no flag axes enumerated" in text
    worlds = json.loads(render_json(_report()))["coverage"]["worlds"]
    assert worlds == {"worlds_enumerated": 1, "axes_enumerated": []}


def test_world_coverage_reports_the_enumerated_worlds_and_axes() -> None:
    report = _report(worlds=WorldCoverage(worlds_enumerated=4, axes_enumerated=("currency", "env")))
    assert "worlds: 4 enumerated over axes: currency, env" in render_text(report)
    worlds = json.loads(render_json(report))["coverage"]["worlds"]
    assert worlds == {"worlds_enumerated": 4, "axes_enumerated": ["currency", "env"]}


def test_resolution_with_nothing_to_resolve_reads_as_not_applicable() -> None:
    text = render_text(_report())
    assert "resolution: n/a" in text
    assert json.loads(render_json(_report()))["coverage"]["resolution"]["fraction"] is None


def test_unbuilt_models_are_surfaced_not_silent() -> None:
    report = _report(unbuilt=(UnbuiltModel("model.shop.weird", "sqlglot: Unknown column: x"),))
    text = render_text(report)
    assert "could not analyze" in text
    assert "model.shop.weird" in text
    assert "1 model(s) could not be analyzed" in text
    # models_analyzed discounts the unbuilt one, so the count never overstates coverage.
    assert "over 2 models" in text
    payload = json.loads(render_json(report))
    assert payload["summary"]["unbuilt"] == 1
    assert payload["summary"]["models_analyzed"] == 2
    assert payload["unbuilt"][0]["unique_id"] == "model.shop.weird"
