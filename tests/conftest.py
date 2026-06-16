"""Shared pytest fixtures for the dblect test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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
