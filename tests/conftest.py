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
