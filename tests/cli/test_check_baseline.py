"""End-to-end tests for ``dblect check --base-manifest``, over the demo scenarios.

The diff analyses the base revision's manifest the same way as HEAD and keeps only the
findings whose cross-world identity is new. The ``currency_creep`` scenario is the case
that shows why this beats scoping a report by edited source line: its findings are an
upstream contradiction on ``stg_payments`` and a downstream one on ``order_revenue``, a
rollup with no contract of its own that the contradiction rides the DAG into. A
developer who adds that rollup never edits the line the downstream finding sits on, yet
it is genuinely new, and the manifest diff surfaces it while staying quiet about the
upstream contradiction the project already carried. The pure diff contract is pinned
without a manifest in ``tests/test_baseline.py``; this module pins the CLI wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from dblect.cli import app

from ._output import plain

_CASES = Path(__file__).parent.parent / "fixtures" / "scenarios" / "cases"
_CURRENCY_CREEP = _CASES / "currency_creep"
_HEAD_MANIFEST = _CURRENCY_CREEP / "manifest.json"

_UPSTREAM = ("domain_type_contradiction", "model.jaffle_shop.stg_payments", "amount")
_DOWNSTREAM = ("domain_type_contradiction", "model.jaffle_shop.order_revenue", "revenue")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def base_without_mart(tmp_path: Path) -> str:
    """A base manifest equal to currency_creep's but lacking the ``order_revenue``
    mart, standing in for the revision before the developer added that rollup. Under
    the same declarations its only finding is the preexisting upstream contradiction.
    """
    raw = json.loads(_HEAD_MANIFEST.read_text())
    raw["nodes"] = {k: v for k, v in raw["nodes"].items() if not k.endswith(".order_revenue")}
    base = tmp_path / "base_manifest.json"
    base.write_text(json.dumps(raw))
    return str(base)


def _run(runner: CliRunner, *extra: str) -> Result:
    return runner.invoke(
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


def _kinds(result: Result) -> set[tuple[str, str | None, str | None]]:
    assert result.exit_code in (0, 1), result.output
    payload = json.loads(result.stdout)
    return {(f["kind"], f["model_unique_id"], f["column"]) for f in payload["findings"]}


def test_diff_filters_preexisting_and_surfaces_introduced(
    runner: CliRunner, base_without_mart: str
) -> None:
    # The full report carries both findings; against a base that lacks the new mart,
    # the preexisting upstream contradiction is filtered and only the downstream
    # blast-radius finding (the one a source-line diff would miss) survives.
    assert _kinds(_run(runner, "--no-fail")) == {_UPSTREAM, _DOWNSTREAM}
    assert _kinds(_run(runner, "--no-fail", "--base-manifest", base_without_mart)) == {_DOWNSTREAM}


def test_identical_base_filters_everything_and_exits_zero(runner: CliRunner) -> None:
    # Base equal to HEAD: every finding is preexisting, so the diff is empty and the
    # run exits 0 even without --no-fail, proving the exit code tracks the filtered set.
    result = _run(runner, "--base-manifest", str(_HEAD_MANIFEST))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["findings"] == []


def test_an_introduced_finding_fails_the_run(runner: CliRunner, base_without_mart: str) -> None:
    assert _run(runner, "--base-manifest", base_without_mart).exit_code == 1


def test_missing_base_manifest_is_an_error(runner: CliRunner, tmp_path: Path) -> None:
    result = _run(runner, "--base-manifest", str(tmp_path / "nope.json"))
    assert result.exit_code != 0
    assert "--base-manifest" in plain(result.output)
