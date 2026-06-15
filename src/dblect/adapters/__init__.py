"""Target adapters: one :class:`AdapterProfile` per dbt adapter, behind a registry.

A dbt project compiles against one adapter (duckdb, snowflake, bigquery, ...), and
that single choice fixes everything dblect reasons about the target: which sqlglot
dialect parses its compiled SQL, whether the warehouse enforces PRIMARY KEY /
UNIQUE and NOT NULL on write, and which incremental strategy runs when a model
leaves ``incremental_strategy`` unset. :class:`AdapterProfile` gathers those facets
into one value so a run reads a single coherent target.

Adding a warehouse is self-contained: a module that builds an ``AdapterProfile``
and calls :func:`register`. The built-ins under :mod:`dblect.adapters.builtin`
are auto-discovered, so this package never enumerates them.

An adapter is **validated** when dblect's detectors have been exercised against its
SQL end-to-end (today, only duckdb). The others carry the runtime semantics dbt's
adapter docs describe and route through the matching sqlglot dialect once the
operator opts in via ``--dialect``.
"""

from __future__ import annotations

from dblect.adapters.model import DEDUP_STRATEGIES, AdapterProfile, IncrementalStrategy
from dblect.adapters.registry import (
    UnvalidatedAdapterError,
    profile_for_adapter,
    register,
    resolve_profile,
    validated_adapters,
)

__all__ = [
    "DEDUP_STRATEGIES",
    "AdapterProfile",
    "IncrementalStrategy",
    "UnvalidatedAdapterError",
    "profile_for_adapter",
    "register",
    "resolve_profile",
    "validated_adapters",
]
