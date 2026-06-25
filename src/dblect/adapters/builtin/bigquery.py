from __future__ import annotations

from dblect.adapters import AdapterProfile, IncrementalStrategy, register
from dblect.sql import PORTABLE_NON_DETERMINISTIC_BUILTINS

# BigQuery's non-deterministic builtins (CURRENT_*, GENERATE_UUID, RAND, SESSION_USER)
# all parse to typed sqlglot nodes, so they are caught dialect-neutrally by the typed
# set in `dblect.sql.patterns`; none arrive as an Anonymous name that would need listing
# here on top of the portable baseline.
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
