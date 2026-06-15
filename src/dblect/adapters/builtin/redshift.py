"""Redshift: PRIMARY KEY / UNIQUE advisory, NOT NULL enforced. Like Postgres,
dbt-redshift defaults to ``delete+insert`` once a ``unique_key`` is set."""

from __future__ import annotations

from dblect.adapters import AdapterProfile, IncrementalStrategy, register

register(
    AdapterProfile(
        adapter_type="redshift",
        sqlglot_dialect="redshift",
        validated=False,
        not_null_enforced=True,
        key_enforced=False,
        default_incremental_strategy=IncrementalStrategy.DELETE_INSERT,
    )
)
