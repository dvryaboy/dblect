"""End-to-end tests for ``dblect init`` and ``dblect check``.

These exercise the CLI plumbing: ``init`` scaffolds the declaration tree and writes
the generated stubs, ``check`` loads a project's contracts, runs the pipeline, and
exits non-zero when findings exist. The finding logic itself is pinned in
``tests/check``; here we confirm the wiring, formats, and exit codes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dblect.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


# --- init -----------------------------------------------------------------------


def test_init_scaffolds_tree_and_writes_stubs(
    jaffle_manifest_path: Path, runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--manifest", str(jaffle_manifest_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "dblect" / "__init__.py").exists()
    assert (tmp_path / "dblect" / "types.py").exists()
    assert (tmp_path / "dblect" / "contracts" / "__init__.py").exists()
    stubs = (tmp_path / "dblect" / "_stubs" / "models.py").read_text()
    assert "class _StgPayments(ModelProxy):" in stubs
    gitignore = (tmp_path / "dblect" / ".gitignore").read_text()
    assert "_stubs" in gitignore


def test_init_does_not_clobber_user_files(
    jaffle_manifest_path: Path, runner: CliRunner, tmp_path: Path
) -> None:
    _write(tmp_path / "dblect" / "types.py", "# my types\n")
    result = runner.invoke(app, ["init", str(tmp_path), "--manifest", str(jaffle_manifest_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "dblect" / "types.py").read_text() == "# my types\n"


# --- check ----------------------------------------------------------------------


def _project_with_contract(tmp_path: Path, body: str) -> Path:
    _write(tmp_path / "dblect" / "__init__.py", "")
    _write(tmp_path / "dblect" / "contracts.py", body)
    return tmp_path


def test_check_clean_project_exits_zero(
    jaffle_manifest_path: Path, runner: CliRunner, tmp_path: Path
) -> None:
    project = _project_with_contract(
        tmp_path,
        "from dblect import ModelContract\n"
        "from dblect.demo import Money, Currency\n"
        "class StgPayments(ModelContract):\n"
        "    dbt_model = 'stg_payments'\n"
        "    amount: Money.refine(currency=Currency.USD)\n",
    )
    result = runner.invoke(app, ["check", str(project), "--manifest", str(jaffle_manifest_path)])
    assert result.exit_code == 0, result.output
    assert "0 findings" in result.output


def test_check_contract_issue_exits_non_zero(
    jaffle_manifest_path: Path, runner: CliRunner, tmp_path: Path
) -> None:
    project = _project_with_contract(
        tmp_path,
        "from dblect import ModelContract\n"
        "from dblect.demo import Money, Currency\n"
        "class Ghost(ModelContract):\n"
        "    dbt_model = 'does_not_exist'\n"
        "    amount: Money.refine(currency=Currency.USD)\n",
    )
    result = runner.invoke(app, ["check", str(project), "--manifest", str(jaffle_manifest_path)])
    assert result.exit_code == 1
    assert "contract_issue" in result.output


def test_check_no_fail_override_exits_zero(
    jaffle_manifest_path: Path, runner: CliRunner, tmp_path: Path
) -> None:
    project = _project_with_contract(
        tmp_path,
        "from dblect import ModelContract\n"
        "from dblect.demo import Money, Currency\n"
        "class Ghost(ModelContract):\n"
        "    dbt_model = 'does_not_exist'\n"
        "    amount: Money.refine(currency=Currency.USD)\n",
    )
    result = runner.invoke(
        app,
        ["check", str(project), "--manifest", str(jaffle_manifest_path), "--no-fail"],
    )
    assert result.exit_code == 0, result.output


def test_check_json_format(jaffle_manifest_path: Path, runner: CliRunner, tmp_path: Path) -> None:
    import json

    project = _project_with_contract(
        tmp_path,
        "from dblect import ModelContract\n"
        "from dblect.demo import Money, Currency\n"
        "class StgPayments(ModelContract):\n"
        "    dbt_model = 'stg_payments'\n"
        "    amount: Money.refine(currency=Currency.USD)\n",
    )
    result = runner.invoke(
        app,
        ["check", str(project), "--manifest", str(jaffle_manifest_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "2"
    assert payload["summary"]["contracts_resolved"] == 1
    # The coverage block rides alongside the summary in the schema.
    assert "resolution" in payload["coverage"]
    assert "grounding" in payload["coverage"]


# --- catalog wiring -------------------------------------------------------------

_MINIMAL_CATALOG = """{
  "metadata": {
    "dbt_schema_version": "https://schemas.getdbt.com/dbt/catalog/v1.json",
    "dbt_version": "1.8.0",
    "generated_at": "2024-01-01T00:00:00Z",
    "invocation_id": "x",
    "env": {}
  },
  "nodes": {},
  "sources": {},
  "errors": null
}
"""


def test_check_reads_catalog_when_supplied(
    jaffle_manifest_path: Path, runner: CliRunner, tmp_path: Path
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(_MINIMAL_CATALOG)
    result = runner.invoke(
        app,
        [
            "check",
            str(tmp_path),
            "--manifest",
            str(jaffle_manifest_path),
            "--catalog",
            str(catalog),
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"reading catalog at {catalog}" in result.output


def test_check_notes_when_no_catalog_is_available(
    jaffle_manifest_path: Path, runner: CliRunner, tmp_path: Path
) -> None:
    # The jaffle manifest has no catalog.json beside it, so the run proceeds and
    # says so rather than silently resolving leaves only from schema.yml.
    result = runner.invoke(app, ["check", str(tmp_path), "--manifest", str(jaffle_manifest_path)])
    assert result.exit_code == 0, result.output
    assert "no catalog.json" in result.output


def test_check_rejects_a_missing_catalog_path(
    jaffle_manifest_path: Path, runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "check",
            str(tmp_path),
            "--manifest",
            str(jaffle_manifest_path),
            "--catalog",
            str(tmp_path / "nope.json"),
        ],
    )
    assert result.exit_code != 0
    assert "catalog path does not exist" in result.output
