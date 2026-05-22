"""Tests for the adapter -> sqlglot dialect resolver."""

from __future__ import annotations

import pytest

from dblect.sql.dialects import UnvalidatedAdapterError, resolve_dialect


def test_validated_adapter_resolves_to_mapped_dialect() -> None:
    assert resolve_dialect(adapter_type="duckdb", explicit_dialect=None) == "duckdb"


@pytest.mark.parametrize("adapter", ["duckdb", "snowflake", "bigquery"])
def test_explicit_dialect_wins_regardless_of_adapter(adapter: str) -> None:
    # The flag itself is the operator's acknowledgment; we don't second-guess
    # it whether the adapter is validated, unvalidated, or unknown.
    assert (
        resolve_dialect(adapter_type=adapter, explicit_dialect="redshift") == "redshift"
    )


def test_unvalidated_adapter_without_override_raises() -> None:
    with pytest.raises(UnvalidatedAdapterError) as exc_info:
        resolve_dialect(adapter_type="snowflake", explicit_dialect=None)
    assert exc_info.value.adapter_type == "snowflake"
