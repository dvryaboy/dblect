"""Rendering a ``CheckReport`` to text and JSON.

The text format is for terminals (a finding per model, click-through path); the
JSON format is a versioned, stable schema for CI and editors. These pin the shape
both consumers depend on.
"""

from __future__ import annotations

import json

from dblect.check import CheckFinding, CheckFindingKind, CheckReport, render_json, render_text
from dblect.loader import LoadIssue


def _report(*findings: CheckFinding, load_issues: tuple[LoadIssue, ...] = ()) -> CheckReport:
    return CheckReport(
        findings=findings,
        load_issues=load_issues,
        contracts_resolved=2,
        models_propagated=3,
        predicates_collected=1,
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
    assert payload["schema_version"] == "1"
    assert payload["summary"] == {
        "contracts_resolved": 2,
        "models_propagated": 3,
        "predicates_collected": 1,
        "findings": 1,
        "load_issues": 1,
    }
    assert payload["findings"][0]["kind"] == "domain_type_contradiction"
    assert payload["findings"][0]["column"] == "amount"
    assert payload["load_issues"][0]["module"] == "m"
