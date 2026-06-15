"""The value types describing one target adapter: :class:`AdapterProfile` and the
incremental strategies it can default to.

These carry no per-adapter data themselves; the concrete profiles live in
self-contained modules under :mod:`dblect.adapters.builtin` and register
themselves with the registry, so adding a warehouse never edits this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IncrementalStrategy(StrEnum):
    """The incremental materialization strategies dbt ships.

    A custom strategy (a project-defined macro) is outside this closed set;
    :meth:`parse` maps an unrecognized value to ``None`` so a caller reads it as
    "no known dedup guarantee" rather than guessing.
    """

    APPEND = "append"
    MERGE = "merge"
    DELETE_INSERT = "delete+insert"
    INSERT_OVERWRITE = "insert_overwrite"
    MICROBATCH = "microbatch"

    @classmethod
    def parse(cls, raw: str | None) -> IncrementalStrategy | None:
        """The strategy a config string names, or ``None`` when unset or custom."""
        if raw is None:
            return None
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return None


# Strategies whose write deduplicates on ``unique_key``: ``merge`` updates the
# matching row and ``delete+insert`` removes then reinserts, so each key value
# lands once. ``append`` inserts unconditionally and ``insert_overwrite`` replaces
# whole partitions, so neither enforces the key.
DEDUP_STRATEGIES: frozenset[IncrementalStrategy] = frozenset(
    {IncrementalStrategy.MERGE, IncrementalStrategy.DELETE_INSERT}
)


@dataclass(frozen=True, slots=True)
class AdapterProfile:
    """Everything dblect's analysis needs to know about one target adapter.

    ``adapter_type`` is the effective target's dbt name (the manifest's, or the one
    an override selects); ``sqlglot_dialect`` parses its compiled SQL. The two
    enforcement flags are descriptive provenance, read by the unenforced-constraint
    findings and never by fact resolution. ``default_incremental_strategy`` is the
    strategy in force when a model leaves ``incremental_strategy`` unset, or
    ``None`` where dblect does not know the adapter's default to deduplicate.
    """

    adapter_type: str
    sqlglot_dialect: str
    validated: bool
    not_null_enforced: bool
    key_enforced: bool
    default_incremental_strategy: IncrementalStrategy | None

    def effective_strategy(self, declared: str | None) -> IncrementalStrategy | None:
        """The strategy in force for a model: the declared one when set (``None`` if
        that is a custom strategy dblect does not recognize, so no default is
        assumed for a model that did choose a strategy), else this adapter's
        default. The default branch is meaningful only when a ``unique_key`` is
        present, which the config discoverer checks before consulting it.
        """
        if declared is not None:
            return IncrementalStrategy.parse(declared)
        return self.default_incremental_strategy
