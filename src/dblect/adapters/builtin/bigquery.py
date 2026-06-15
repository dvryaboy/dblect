from __future__ import annotations

from dblect.adapters import AdapterProfile, IncrementalStrategy, register

register(
    AdapterProfile(
        adapter_type="bigquery",
        sqlglot_dialect="bigquery",
        validated=False,
        not_null_enforced=True,
        key_enforced=False,
        default_incremental_strategy=IncrementalStrategy.MERGE,
    )
)
