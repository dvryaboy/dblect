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
