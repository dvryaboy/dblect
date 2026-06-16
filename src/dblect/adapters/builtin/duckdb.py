from __future__ import annotations

from dblect.adapters import AdapterProfile, register
from dblect.sql import PORTABLE_NON_DETERMINISTIC_BUILTINS

# `txid_current()` and `nextval()` arrive as `exp.Anonymous`. `random()`, `uuid()`,
# `today()` and `gen_random_uuid()` are usually normalised to a typed node (so
# already caught dialect-neutrally) and are listed for robustness, so a sqlglot
# version that leaves them anonymous still fires.
_DUCKDB_NON_DETERMINISTIC_BUILTINS = PORTABLE_NON_DETERMINISTIC_BUILTINS | frozenset(
    {"random", "uuid", "txid_current", "nextval", "today", "gen_random_uuid"}
)

register(
    AdapterProfile(
        adapter_type="duckdb",
        sqlglot_dialect="duckdb",
        validated=True,
        not_null_enforced=True,
        key_enforced=True,
        default_incremental_strategy=None,  # left unset pending validation
        non_deterministic_builtins=_DUCKDB_NON_DETERMINISTIC_BUILTINS,
    )
)
