"""The target system dblect analyzes: one profile per dbt adapter.

A dbt project compiles against one adapter (duckdb, snowflake, bigquery, ...), and
that single choice fixes everything dblect needs to know about the target: which
sqlglot dialect parses its compiled SQL, whether the warehouse enforces PRIMARY
KEY / UNIQUE and NOT NULL on write, and which incremental strategy runs when a
model leaves ``incremental_strategy`` unset. :class:`AdapterProfile` gathers those
facets into one value, so a run reads a single coherent target instead of
assembling it from independent per-facet lookups.

A dbt adapter name and a sqlglot dialect name are two namespaces that overlap by
name without being the same thing (a custom adapter may share Snowflake's SQL
grammar, for instance). The profile carries both, and :func:`resolve_profile` is
the one place a run commits to a target: an explicit override names that target
wholesale, so its grammar and its runtime semantics always agree.

An adapter is **validated** when dblect's detectors have been exercised against
its SQL and we trust the results. Only duckdb is validated today. The others carry
the runtime semantics dbt's adapter docs describe and route through the matching
sqlglot dialect once the operator opts in via ``--dialect``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


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


# The adapters dblect knows specifically. NOT NULL is enforced on write by
# essentially every warehouse, so it is true throughout; PRIMARY KEY / UNIQUE is
# advisory on the cloud warehouses (Snowflake, BigQuery, Redshift check neither)
# and enforced on duckdb and Postgres. The default incremental strategy follows
# dbt's adapter docs: Snowflake and BigQuery default to ``merge``, Postgres and
# Redshift to ``delete+insert`` once a ``unique_key`` is set. duckdb's dedup
# default is left unset pending validation, so an unset strategy there claims no
# key, the conservative reading.
_KNOWN: Mapping[str, AdapterProfile] = MappingProxyType(
    {
        "duckdb": AdapterProfile(
            adapter_type="duckdb",
            sqlglot_dialect="duckdb",
            validated=True,
            not_null_enforced=True,
            key_enforced=True,
            default_incremental_strategy=None,
        ),
        "postgres": AdapterProfile(
            adapter_type="postgres",
            sqlglot_dialect="postgres",
            validated=False,
            not_null_enforced=True,
            key_enforced=True,
            default_incremental_strategy=IncrementalStrategy.DELETE_INSERT,
        ),
        "redshift": AdapterProfile(
            adapter_type="redshift",
            sqlglot_dialect="redshift",
            validated=False,
            not_null_enforced=True,
            key_enforced=False,
            default_incremental_strategy=IncrementalStrategy.DELETE_INSERT,
        ),
        "snowflake": AdapterProfile(
            adapter_type="snowflake",
            sqlglot_dialect="snowflake",
            validated=False,
            not_null_enforced=True,
            key_enforced=False,
            default_incremental_strategy=IncrementalStrategy.MERGE,
        ),
        "bigquery": AdapterProfile(
            adapter_type="bigquery",
            sqlglot_dialect="bigquery",
            validated=False,
            not_null_enforced=True,
            key_enforced=False,
            default_incremental_strategy=IncrementalStrategy.MERGE,
        ),
    }
)

VALIDATED_ADAPTERS: frozenset[str] = frozenset(a for a, p in _KNOWN.items() if p.validated)


def _conservative(adapter_type: str, *, sqlglot_dialect: str | None = None) -> AdapterProfile:
    """The profile for an adapter dblect has no specific knowledge of: NOT NULL
    enforced (true on essentially every warehouse), PRIMARY KEY / UNIQUE advisory,
    and no known dedup default, so an unset incremental strategy claims no key."""
    return AdapterProfile(
        adapter_type=adapter_type,
        sqlglot_dialect=sqlglot_dialect if sqlglot_dialect is not None else adapter_type,
        validated=False,
        not_null_enforced=True,
        key_enforced=False,
        default_incremental_strategy=None,
    )


def profile_for_adapter(adapter_type: str) -> AdapterProfile:
    """The capability profile for a dbt adapter by name.

    An adapter dblect has no specific knowledge of gets a conservative profile,
    never an error: this is the semantics lookup, distinct from the
    parsing-validation gate in :func:`resolve_profile`.
    """
    return _KNOWN.get(adapter_type.strip().lower()) or _conservative(adapter_type)


class UnvalidatedAdapterError(ValueError):
    """The manifest's adapter is not in dblect's validated set and no ``--dialect``
    override is in effect. Carries the adapter name so the CLI can build an
    actionable message."""

    def __init__(self, adapter_type: str) -> None:
        super().__init__(
            f"adapter `{adapter_type}` is not in dblect's validated set "
            f"({sorted(VALIDATED_ADAPTERS)})"
        )
        self.adapter_type = adapter_type


def resolve_profile(*, adapter_type: str, explicit_dialect: str | None) -> AdapterProfile:
    """The single target profile for a run, or raise :class:`UnvalidatedAdapterError`.

    An ``explicit_dialect`` override names the target wholesale (its grammar and its
    runtime semantics together), so the two cannot drift apart; passing it is the
    operator's acknowledgment of a best-effort, possibly unvalidated
    interpretation. Without an override the manifest's adapter must be validated.
    """
    if explicit_dialect is not None:
        return profile_for_adapter(explicit_dialect)
    profile = profile_for_adapter(adapter_type)
    if not profile.validated:
        raise UnvalidatedAdapterError(adapter_type)
    return profile
