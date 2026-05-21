"""End-to-end tests for the DuckDB execution harness.

These tests require ``dbt-core`` and ``dbt-duckdb`` to be importable; they
skip otherwise. The ``dbt`` CLI must also be on ``PATH``. Both are pinned in
dblect's dev dependency group, so CI installs them automatically.

The vendored project under ``tests/fixtures/jaffle_project/`` is the
canonical exercise; everything else is a variation on its inputs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("dbt")


@pytest.fixture(scope="session")
def jaffle_project_dir() -> Path:
    path = Path(__file__).parent.parent / "fixtures" / "jaffle_project"
    if not (path / "dbt_project.yml").exists():
        pytest.skip(f"jaffle_project fixture missing at {path}")
    if shutil.which("dbt") is None:
        pytest.skip("dbt CLI not on PATH")
    return path


def test_runs_customers_against_real_seeds(jaffle_project_dir: Path) -> None:
    from dblect.execution import run_model

    result = run_model(jaffle_project_dir, "customers")
    assert result.model_name == "customers"
    assert result.row_count == 100
    assert "customer_id" in result.columns
    assert "customer_lifetime_value" in result.columns


def test_empty_seeds_produce_empty_output(jaffle_project_dir: Path) -> None:
    from dblect.execution import run_model

    result = run_model(
        jaffle_project_dir,
        "customers",
        fixtures={"raw_customers": [], "raw_orders": [], "raw_payments": []},
    )
    assert result.row_count == 0
    # Schema is preserved even when input is empty.
    assert "customer_id" in result.columns


def test_custom_fixture_rows_flow_through(jaffle_project_dir: Path) -> None:
    from dblect.execution import run_model

    fixtures = {
        "raw_customers": [
            {"id": 1, "first_name": "Alice", "last_name": "A."},
            {"id": 2, "first_name": "Bob", "last_name": "B."},
        ],
        "raw_orders": [],
        "raw_payments": [],
    }
    result = run_model(jaffle_project_dir, "customers", fixtures=fixtures)
    assert result.row_count == 2
    dicts = result.as_dicts()
    by_id = {row["customer_id"]: row for row in dicts}
    assert by_id[1]["first_name"] == "Alice"
    assert by_id[2]["first_name"] == "Bob"
    # No orders/payments → derived columns are NULL.
    assert by_id[1]["number_of_orders"] is None
    assert by_id[1]["customer_lifetime_value"] is None


def test_run_error_surfaces_dbt_compile_failure(jaffle_project_dir: Path, tmp_path: Path) -> None:
    from dblect.execution import RunError, run_model

    broken = tmp_path / "broken_project"
    shutil.copytree(jaffle_project_dir, broken)
    # Break customers.sql so dbt-compile / dbt-run fails loudly.
    customers = broken / "models" / "customers.sql"
    customers.write_text("select * from {{ ref('does_not_exist') }}")

    with pytest.raises(RunError) as excinfo:
        run_model(broken, "customers")
    assert excinfo.value.phase in {"run", "seed"}
    assert excinfo.value.returncode != 0


def test_missing_project_dir_raises_file_not_found(tmp_path: Path) -> None:
    from dblect.execution import run_model

    with pytest.raises(FileNotFoundError):
        run_model(tmp_path / "nope", "customers")


def test_keep_artifacts_in_persists_warehouse(jaffle_project_dir: Path, tmp_path: Path) -> None:
    from dblect.execution import run_model

    keep = tmp_path / "kept"
    run_model(jaffle_project_dir, "stg_customers", keep_artifacts_in=keep)
    assert any(p.suffix == ".duckdb" for p in keep.iterdir())
