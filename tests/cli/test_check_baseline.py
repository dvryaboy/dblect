"""End-to-end tests for ``dblect check --base-manifest``, over the demo scenarios.

The base-manifest diff reports the findings a change introduces: it analyses the base
revision's manifest the same way as HEAD and keeps only the findings whose cross-world
identity is new. The ``currency_creep`` scenario is the case that shows why this beats
scoping a report by edited source line. Its findings are an upstream contradiction on
``stg_payments`` (a stale USD contract against a multi-currency source) and a
downstream one on ``order_revenue``, a rollup with no contract of its own that the
contradiction rides the DAG into. A developer who adds that rollup never edits the
line the downstream finding sits on, yet it is genuinely new. The manifest diff
surfaces it while staying quiet about the upstream contradiction the project already
carried, which is exactly the regression a source-line diff would miss.

The pure diff contract (identity ignores line span and message) is pinned without a
manifest in ``tests/test_baseline.py``; this module pins the CLI wiring and the
honest errors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dblect.cli import app

_CASES = Path(__file__).parent.parent / "fixtures" / "scenarios" / "cases"
_CURRENCY_CREEP = _CASES / "currency_creep"
_HEAD_MANIFEST = _CURRENCY_CREEP / "manifest.json"

_UPSTREAM = ("domain_type_contradiction", "model.jaffle_shop.stg_payments", "amount")
_DOWNSTREAM = ("domain_type_contradiction", "model.jaffle_shop.order_revenue", "revenue")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def base_without_mart(tmp_path: Path) -> Path:
    """A base manifest equal to currency_creep's but lacking the ``order_revenue``
    mart, standing in for the revision before the developer added that rollup. Under
    the same declarations its only finding is the preexisting upstream contradiction.
    """
    raw = json.loads(_HEAD_MANIFEST.read_text())
    raw["nodes"] = {k: v for k, v in raw["nodes"].items() if not k.endswith(".order_revenue")}
    base = tmp_path / "base_manifest.json"
    base.write_text(json.dumps(raw))
    return base


def _findings(runner: CliRunner, *extra: str) -> set[tuple[str, str | None, str | None]]:
    result = runner.invoke(
        app,
        [
            "check",
            str(_CURRENCY_CREEP),
            "--manifest",
            str(_HEAD_MANIFEST),
            "--format",
            "json",
            *extra,
        ],
    )
    assert result.exit_code in (0, 1), result.output
    payload = json.loads(result.stdout)
    return {(f["kind"], f["model_unique_id"], f["column"]) for f in payload["findings"]}


def test_full_report_without_base_lists_both_findings(runner: CliRunner) -> None:
    assert _findings(runner, "--no-fail") == {_UPSTREAM, _DOWNSTREAM}


def test_base_diff_surfaces_only_the_introduced_downstream_finding(
    runner: CliRunner, base_without_mart: Path
) -> None:
    # The upstream contradiction is in the base, so it is preexisting and filtered;
    # the new downstream blast-radius finding on order_revenue is what survives.
    assert _findings(runner, "--no-fail", "--base-manifest", str(base_without_mart)) == {
        _DOWNSTREAM
    }


def test_identical_base_reports_nothing(runner: CliRunner) -> None:
    # Base equal to HEAD: every finding is preexisting, so the diff is empty and the
    # run exits 0 even without --no-fail.
    result = runner.invoke(
        app,
        [
            "check",
            str(_CURRENCY_CREEP),
            "--manifest",
            str(_HEAD_MANIFEST),
            "--format",
            "json",
            "--base-manifest",
            str(_HEAD_MANIFEST),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["findings"] == []


def test_introduced_finding_fails_the_run(runner: CliRunner, base_without_mart: Path) -> None:
    # An introduced finding still fails the build like any other finding.
    result = runner.invoke(
        app,
        [
            "check",
            str(_CURRENCY_CREEP),
            "--manifest",
            str(_HEAD_MANIFEST),
            "--base-manifest",
            str(base_without_mart),
        ],
    )
    assert result.exit_code == 1, result.output


def test_missing_base_manifest_is_an_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "check",
            str(_CURRENCY_CREEP),
            "--manifest",
            str(_HEAD_MANIFEST),
            "--base-manifest",
            str(tmp_path / "nope.json"),
        ],
    )
    assert result.exit_code != 0
    assert "--base-manifest" in result.output


def test_base_catalog_without_base_manifest_is_an_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "check",
            str(_CURRENCY_CREEP),
            "--manifest",
            str(_HEAD_MANIFEST),
            "--base-catalog",
            str(tmp_path / "catalog.json"),
        ],
    )
    assert result.exit_code != 0
    assert "--base-catalog" in result.output
