"""Fixtures shared by the lineage soundness PBTs.

The empirical soundness PBTs each run hundreds of Hypothesis examples, and every
example materializes a tiny model in duckdb. Opening a fresh in-memory database per
example dominated their runtime, so the examples share one connection for the whole
session and clean their tables up between materializations (see ``_duckdb_oracle``).
"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest


@pytest.fixture(scope="session")
def oracle_con() -> Iterator[duckdb.DuckDBPyConnection]:
    """One in-memory duckdb connection reused across every soundness-PBT example."""
    con = duckdb.connect(":memory:")
    try:
        yield con
    finally:
        con.close()
