from __future__ import annotations

from dblect.adapters import AdapterProfile, IncrementalStrategy, register

register(
    AdapterProfile(
        adapter_type="redshift",
        sqlglot_dialect="redshift",
        validated=False,
        not_null_enforced=True,
        key_enforced=False,
        # dbt-redshift, like Postgres, defaults to delete+insert (not merge) with a unique_key
        default_incremental_strategy=IncrementalStrategy.DELETE_INSERT,
    )
)
