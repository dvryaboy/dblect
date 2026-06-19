"""Shared pytest fixtures for the dblect test suite."""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def dbt_cli() -> str:
    """The dbt CLI to drive the execution-harness tests, or skip if it is absent.

    Under ``uv run`` the console script lives in the project venv and is on PATH, so
    these tests run in the normal workflow; outside it (a bare ``python`` with no dbt
    installed) they skip. Set ``DBLECT_REQUIRE_DBT`` to turn the skip into a failure,
    so CI cannot silently drop this coverage when the dbt install regresses. Tests
    pass the returned path as ``dbt_executable`` so they exercise the resolved
    interpreter rather than whatever bare ``dbt`` PATH happens to hold.
    """
    importable = importlib.util.find_spec("dbt") is not None
    executable = shutil.which("dbt")
    if importable and executable is not None:
        return executable
    reason = (
        "dbt unavailable: needs dbt-core/dbt-duckdb importable and the dbt CLI on PATH "
        "(run under `uv run pytest`)"
    )
    if os.environ.get("DBLECT_REQUIRE_DBT"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def jaffle_manifest_path() -> Path:
    """Absolute path to the vendored jaffle_shop_duckdb manifest.json."""
    path = FIXTURES / "jaffle" / "manifest.json"
    if not path.exists():
        pytest.skip(
            "jaffle fixture not present; run scripts/refresh_jaffle_fixtures.sh",
        )
    return path


@pytest.fixture(scope="session")
def snapshot_audit_manifest_path() -> Path:
    """Manifest of the snapshot-audit fixture: two snapshots (one with default
    validity column names, one renaming them via snapshot_meta_column_names) and
    consumer models reading them safely and unsafely. Backs the end-to-end test
    that the snapshot temporal-filter detector fires on real dbt-compiled SQL.
    """
    path = FIXTURES / "snapshot_audit" / "manifest.json"
    if not path.exists():
        pytest.skip(
            "snapshot-audit fixture not present; run scripts/refresh_snapshot_audit.sh",
        )
    return path


@pytest.fixture(scope="session")
def jaffle_snowflake_meta_manifest_path() -> Path:
    """Jaffle manifest with ``metadata.adapter_type`` relabeled to ``snowflake``.

    Same SQL as the regular jaffle fixture; only the adapter field differs. Used
    to exercise the unvalidated-adapter gate without running a real Snowflake
    build alongside the duckdb one.
    """
    path = FIXTURES / "jaffle_snowflake_meta" / "manifest.json"
    if not path.exists():
        pytest.skip(
            "snowflake-relabeled jaffle fixture not present; "
            "run scripts/refresh_jaffle_fixtures.sh",
        )
    return path
