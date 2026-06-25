from __future__ import annotations

from dblect.adapters import AdapterProfile, IncrementalStrategy, register
from dblect.sql import PORTABLE_NON_DETERMINISTIC_BUILTINS

register(
    AdapterProfile(
        adapter_type="bigquery",
        sqlglot_dialect="bigquery",
        validated=True,
        # NOT NULL is enforced (REQUIRED mode). PRIMARY KEY / FOREIGN KEY constraints
        # exist but are advisory (unenforced), so keys are not enforced.
        not_null_enforced=True,
        key_enforced=False,
        default_incremental_strategy=IncrementalStrategy.MERGE,
        non_deterministic_builtins=PORTABLE_NON_DETERMINISTIC_BUILTINS,
    )
)
