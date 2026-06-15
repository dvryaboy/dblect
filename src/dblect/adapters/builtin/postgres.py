"""Postgres: enforces PRIMARY KEY / UNIQUE and NOT NULL on write.

Its incremental default deduplicates once a ``unique_key`` is set: dbt-postgres
defaults to ``delete+insert`` with a key (and ``append`` without one, but the
config discoverer only consults the default when a key is present).
"""

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
