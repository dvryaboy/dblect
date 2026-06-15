"""BigQuery: PRIMARY KEY / UNIQUE advisory, NOT NULL enforced; incremental default
``merge``, which deduplicates on a ``unique_key``."""

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
