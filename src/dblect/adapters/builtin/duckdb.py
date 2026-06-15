"""duckdb: the one adapter dblect has validated end-to-end.

duckdb enforces PRIMARY KEY / UNIQUE and NOT NULL on write. Its incremental dedup
default is left unset pending validation, so an unset strategy claims no key, the
conservative reading.
"""

from __future__ import annotations

from dblect.adapters import AdapterProfile, register

register(
    AdapterProfile(
        adapter_type="duckdb",
        sqlglot_dialect="duckdb",
        validated=True,
        not_null_enforced=True,
        key_enforced=True,
        default_incremental_strategy=None,
    )
)
