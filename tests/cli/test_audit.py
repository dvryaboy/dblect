"""End-to-end tests for the ``dblect audit`` CLI command."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dblect.cli import app


@pytest.fixture
def runner() -> CliRunner:
    # mix_stderr=False so we can inspect stderr separately when needed.
    return CliRunner()


def test_audit_with_explicit_manifest(jaffle_manifest_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["audit", "--manifest", str(jaffle_manifest_path), "."])
    assert result.exit_code == 0, result.output
    assert "models/customers.sql" in result.output
    assert "null_group_after_outer_join" in result.output


def test_audit_finds_manifest_in_target_dir(
    tmp_path: Path, jaffle_manifest_path: Path, runner: CliRunner
) -> None:
    project = tmp_path / "p"
    (project / "target").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: x\nprofile: x\n")
    shutil.copy(jaffle_manifest_path, project / "target" / "manifest.json")
    result = runner.invoke(app, ["audit", str(project)])
    assert result.exit_code == 0, result.output
    assert "null_group_after_outer_join" in result.output


def test_audit_missing_manifest_and_no_dbt_project_fails(
    tmp_path: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["audit", str(tmp_path)])
    assert result.exit_code != 0
    assert "dbt_project.yml" in result.output


def test_audit_explicit_manifest_missing_fails(tmp_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["audit", "--manifest", str(tmp_path / "nope.json"), "."],
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_version_command_still_works(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()
