from __future__ import annotations

from dblect.adapters import AdapterProfile, IncrementalStrategy, register
from dblect.sql import PORTABLE_NON_DETERMINISTIC_BUILTINS

register(
    AdapterProfile(
        adapter_type="postgres",
        sqlglot_dialect="postgres",
        validated=False,
        not_null_enforced=True,
        key_enforced=True,
        # dbt-postgres defaults to delete+insert (not merge) once a unique_key is set
        default_incremental_strategy=IncrementalStrategy.DELETE_INSERT,
        non_deterministic_builtins=PORTABLE_NON_DETERMINISTIC_BUILTINS,
    )
)
