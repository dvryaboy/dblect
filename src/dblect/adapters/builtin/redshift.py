"""Redshift: PRIMARY KEY / UNIQUE are advisory (declared but unenforced); NOT NULL
is enforced on write.

Like Postgres, its incremental default deduplicates with a ``unique_key``:
dbt-redshift defaults to ``delete+insert`` once a key is set.
"""

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
