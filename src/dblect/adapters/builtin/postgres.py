"""Postgres: enforces PRIMARY KEY / UNIQUE and NOT NULL. dbt-postgres defaults to
``delete+insert`` once a ``unique_key`` is set (the only default the discoverer reads)."""

from __future__ import annotations

from dblect.adapters import AdapterProfile, IncrementalStrategy, register

register(
    AdapterProfile(
        adapter_type="postgres",
        sqlglot_dialect="postgres",
        validated=False,
        not_null_enforced=True,
        key_enforced=True,
        default_incremental_strategy=IncrementalStrategy.DELETE_INSERT,
    )
)
