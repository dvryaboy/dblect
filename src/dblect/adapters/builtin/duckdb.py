from __future__ import annotations

from dblect.adapters import AdapterProfile, register

register(
    AdapterProfile(
        adapter_type="duckdb",
        sqlglot_dialect="duckdb",
        validated=True,
        not_null_enforced=True,
        key_enforced=True,
        default_incremental_strategy=None,  # left unset pending validation
    )
)
