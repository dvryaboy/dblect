"""End-to-end tests for the ``dblect check`` command's structural coverage.

``check`` runs both detector families. These exercise the structural side over the
jaffle manifest (which declares no contracts, so only the structural family fires)
plus the shared plumbing: manifest discovery, the unvalidated-adapter gate, exit
codes, and the JSON schema. The declaration side and init are pinned in
``test_init_and_check.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dblect.cli import app

from ._output import plain as _plain


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_structural_finding_with_explicit_manifest(
    jaffle_manifest_path: Path, runner: CliRunner
) -> None:
    # jaffle has an unsuppressed structural finding, so the default fail-on-findings
    # behaviour makes this exit non-zero. --no-fail lets us assert the report content.
    result = runner.invoke(
        app, ["check", "--manifest", str(jaffle_manifest_path), "--no-fail", "."]
    )
    assert result.exit_code == 0, result.output
    assert "models/customers.sql" in result.output
    assert "null_group_after_outer_join" in result.output


def test_auto_discovers_manifest_under_target(
    jaffle_manifest_path: Path, tmp_path: Path, runner: CliRunner
) -> None:
    # With no --manifest, dblect resolves <project>/target/manifest.json. This is
    # the everyday path for a user who has already run `dbt compile`.
    target = tmp_path / "target"
    target.mkdir()
    shutil.copy(jaffle_manifest_path, target / "manifest.json")
    result = runner.invoke(app, ["check", str(tmp_path), "--no-fail"])
    assert result.exit_code == 0, result.output
    assert "null_group_after_outer_join" in result.output


def test_missing_project_and_manifest_is_actionable(tmp_path: Path, runner: CliRunner) -> None:
    # No manifest and nothing that looks like a dbt project: the error names both
    # ways forward rather than surfacing a resolution stack trace.
    result = runner.invoke(app, ["check", str(tmp_path)])
    assert result.exit_code != 0
    plain = _plain(result.output)
    assert "dbt_project.yml" in plain
    assert "--manifest" in plain


def test_dbt_missing_on_path_points_at_the_extra(tmp_path: Path, runner: CliRunner) -> None:
    # A dbt project with no compiled manifest and no dbt on PATH is the cold-start
    # trap the [dbt-core] extra exists for; the error names the extra instead of
    # failing deep inside a compile that never starts. A bogus --dbt-executable
    # forces the not-on-PATH branch without depending on the host's PATH.
    (tmp_path / "dbt_project.yml").write_text("name: t\n")
    result = runner.invoke(
        app, ["check", str(tmp_path), "--dbt-executable", "dblect-no-such-dbt-binary"]
    )
    assert result.exit_code != 0
    assert "dblect[dbt-core]" in _plain(result.output)


_CLEAN_MANIFEST = (
    Path(__file__).parent.parent
    / "fixtures"
    / "scenarios"
    / "cases"
    / "order_rollup_sound"
    / "manifest.json"
)


def test_clean_project_exits_zero(tmp_path: Path, runner: CliRunner) -> None:
    # A project clean across both families exits 0 without --no-fail: the basic CI
    # contract. The order_rollup_sound scenario has no structural hazard, and tmp_path
    # declares no contracts, so the whole run is genuinely finding-free.
    result = runner.invoke(app, ["check", str(tmp_path), "--manifest", str(_CLEAN_MANIFEST)])
    assert result.exit_code == 0, result.output
    assert "0 findings over" in result.output


def test_exits_non_zero_when_findings_present(
    jaffle_manifest_path: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["check", "--manifest", str(jaffle_manifest_path), "."])
    assert result.exit_code == 1


def test_exits_zero_with_no_fail_override(jaffle_manifest_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["check", "--manifest", str(jaffle_manifest_path), "--no-fail", "."]
    )
    assert result.exit_code == 0


def test_fail_on_error_passes_warn_only_run(jaffle_manifest_path: Path, runner: CliRunner) -> None:
    # jaffle's structural finding is null_group_after_outer_join, an error-level
    # correctness hazard, so --fail-on error still fails it. The warn-vs-error
    # boundary is pinned deterministically in tests/test_fail_threshold.py against
    # synthetic findings; here we pin that the flag is wired into the exit code.
    result = runner.invoke(
        app, ["check", "--manifest", str(jaffle_manifest_path), "--fail-on", "error", "."]
    )
    assert result.exit_code == 1, result.output


def test_fail_on_default_is_warn(jaffle_manifest_path: Path, runner: CliRunner) -> None:
    # No --fail-on given: the default warn threshold fails the error-level finding.
    result = runner.invoke(app, ["check", "--manifest", str(jaffle_manifest_path), "."])
    assert result.exit_code == 1, result.output


def test_no_fail_beats_fail_on(jaffle_manifest_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        [
            "check",
            "--manifest",
            str(jaffle_manifest_path),
            "--fail-on",
            "info",
            "--no-fail",
            ".",
        ],
    )
    assert result.exit_code == 0, result.output


def test_text_report_renders_severity(jaffle_manifest_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["check", "--manifest", str(jaffle_manifest_path), "--no-fail", "."]
    )
    assert result.exit_code == 0, result.output
    # The error-level structural finding shows its level alongside its kind.
    assert "error" in result.output


def test_json_finding_carries_severity(jaffle_manifest_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["check", "--manifest", str(jaffle_manifest_path), "--format", "json", "--no-fail", "."],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    structural = [f for f in payload["findings"] if f["family"] == "structural"]
    assert structural
    assert all(f["severity"] in {"info", "warn", "error"} for f in payload["findings"])


def test_json_format_produces_stable_schema(jaffle_manifest_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["check", "--manifest", str(jaffle_manifest_path), "--format", "json", "--no-fail", "."],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "schema_version" in payload
    from dblect.manifest import Manifest

    expected_models = len(Manifest.from_file(jaffle_manifest_path).models)
    assert payload["summary"]["models_scanned"] == expected_models
    structural = [f for f in payload["findings"] if f["family"] == "structural"]
    assert "null_group_after_outer_join" in {f["kind"] for f in structural}


def test_json_format_still_exits_non_zero_on_findings(
    jaffle_manifest_path: Path, runner: CliRunner
) -> None:
    result = runner.invoke(
        app, ["check", "--manifest", str(jaffle_manifest_path), "--format", "json", "."]
    )
    assert result.exit_code == 1
    # Even on failure, stdout is still a parseable JSON document so CI can consume it.
    json.loads(result.stdout)


def test_finds_manifest_in_target_dir(
    tmp_path: Path, jaffle_manifest_path: Path, runner: CliRunner
) -> None:
    project = tmp_path / "p"
    (project / "target").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: x\nprofile: x\n")
    shutil.copy(jaffle_manifest_path, project / "target" / "manifest.json")
    result = runner.invoke(app, ["check", "--no-fail", str(project)])
    assert result.exit_code == 0, result.output
    assert "null_group_after_outer_join" in result.output


def test_finds_manifest_under_flags_target_path(
    tmp_path: Path, jaffle_manifest_path: Path, runner: CliRunner
) -> None:
    # Recent dbt moves the setting under ``flags:``; honor that location too.
    project = tmp_path / "p"
    (project / "build").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: x\nprofile: x\nflags:\n  target_path: build\n")
    shutil.copy(jaffle_manifest_path, project / "build" / "manifest.json")
    result = runner.invoke(app, ["check", "--no-fail", str(project)])
    assert result.exit_code == 0, result.output
    assert "null_group_after_outer_join" in result.output


def test_relative_target_path_outside_project_resolves(
    tmp_path: Path, jaffle_manifest_path: Path, runner: CliRunner
) -> None:
    # ``target-path: ../docs`` (the case from the field report) lands artifacts in a
    # sibling directory; resolution must walk out of the project dir to find them.
    project = tmp_path / "p"
    project.mkdir(parents=True)
    docs = tmp_path / "docs"
    docs.mkdir()
    (project / "dbt_project.yml").write_text("name: x\nprofile: x\ntarget-path: ../docs\n")
    shutil.copy(jaffle_manifest_path, docs / "manifest.json")
    result = runner.invoke(app, ["check", "--no-fail", str(project)])
    assert result.exit_code == 0, result.output
    assert "null_group_after_outer_join" in result.output


def test_env_var_overrides_project_target_path(
    tmp_path: Path, jaffle_manifest_path: Path, runner: CliRunner
) -> None:
    # dbt's precedence puts DBT_TARGET_PATH above dbt_project.yml; the manifest sits
    # where the env var points, not where the config says, so the env var must win.
    project = tmp_path / "p"
    (project / "from_env").mkdir(parents=True)
    (project / "config_dir").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: x\nprofile: x\ntarget-path: config_dir\n")
    shutil.copy(jaffle_manifest_path, project / "from_env" / "manifest.json")
    result = runner.invoke(
        app, ["check", "--no-fail", str(project)], env={"DBT_TARGET_PATH": "from_env"}
    )
    assert result.exit_code == 0, result.output
    assert "null_group_after_outer_join" in result.output


def test_missing_manifest_and_no_dbt_project_fails(tmp_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "dbt_project.yml" in result.output


def test_explicit_manifest_missing_fails(tmp_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["check", "--manifest", str(tmp_path / "nope.json"), "."])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_version_command_still_works(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_bails_on_unvalidated_adapter(
    jaffle_snowflake_meta_manifest_path: Path, runner: CliRunner
) -> None:
    result = runner.invoke(
        app, ["check", "--manifest", str(jaffle_snowflake_meta_manifest_path), "--no-fail", "."]
    )
    assert result.exit_code != 0
    plain = _plain(result.output)
    assert "snowflake" in plain
    assert "--dialect" in plain


def test_dialect_override_unlocks_unvalidated_adapter(
    jaffle_snowflake_meta_manifest_path: Path, runner: CliRunner
) -> None:
    # Force-interpret the jaffle SQL as duckdb; the override is the operator opt-in,
    # so the run proceeds and lands the usual jaffle structural finding.
    result = runner.invoke(
        app,
        [
            "check",
            "--manifest",
            str(jaffle_snowflake_meta_manifest_path),
            "--dialect",
            "duckdb",
            "--no-fail",
            ".",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "null_group_after_outer_join" in result.output


def test_warns_when_using_unvalidated_dialect(
    jaffle_manifest_path: Path, runner: CliRunner
) -> None:
    result = runner.invoke(
        app,
        [
            "check",
            "--manifest",
            str(jaffle_manifest_path),
            "--dialect",
            "snowflake",
            "--no-fail",
            ".",
        ],
    )
    assert result.exit_code == 0, result.output
    # The override names a target dblect has not validated end-to-end, so the run
    # warns it is best-effort and names the target it fell back to.
    assert "unvalidated target" in result.output
    assert "snowflake" in result.output
