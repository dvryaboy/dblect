"""A fresh contract registry per test, so in-test ``ModelContract`` definitions do
not leak across the check-pipeline tests (mirrors ``tests/types/conftest``)."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest

from dblect.types import ContractRegistry, isolated_registry


@pytest.fixture(autouse=True)
def registry() -> Iterator[ContractRegistry]:
    with isolated_registry() as reg:
        yield reg


@pytest.fixture(scope="session")
def oracle_con() -> Iterator[duckdb.DuckDBPyConnection]:
    """One in-memory duckdb connection reused across the check layer's
    soundness-PBT examples, the same move ``tests/lineage/conftest.py`` makes
    for the lineage layer's own PBTs."""
    con = duckdb.connect(":memory:")
    try:
        yield con
    finally:
        con.close()
