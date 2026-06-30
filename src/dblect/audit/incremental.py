"""Flag dbt incremental models that silently append duplicate rows.

An incremental model without a ``unique_key`` (and without an incremental strategy that
reconciles rows another way) appends every run's output to the existing table. A
backfill, a rerun, or late-arriving data then lands the same logical row twice, a
duplicate the model's own SQL never reveals because each run is internally correct. dbt's
docs call this out; this raises it at audit time from the manifest config alone, with no
SQL parse needed.

The decision is over the node's resolved ``config``: the materialization, the incremental
strategy, and whether a ``unique_key`` is declared. The strategy space is classified into
the append-style ones that need a key to stay idempotent (``append``, ``merge``, and
``delete+insert``, plus the unset default, which every adapter resolves to one of these)
and the ones that reconcile rows without a key (``insert_overwrite`` replaces a partition
or the whole table; ``microbatch`` rebuilds per time batch). A strategy dblect does not
recognize (a custom macro-defined one) is left alone: its idempotency is unknown, so the
firewall stays silent rather than guess.

The finding is model-scoped (line 0): the concern is the model's configuration as a
whole, and the ``config()`` block is stripped from the compiled SQL the audit parses, so
there is no SQL line to anchor it to.
"""

from __future__ import annotations

from enum import Enum, StrEnum, auto
from typing import assert_never

from dblect.manifest import Materialization, Node
from dblect.sql import Finding, FindingKind


class IncrementalStrategy(StrEnum):
    """A dbt ``incremental_strategy``, normalized to the names dblect classifies.

    The named strategies dbt ships across its adapters, plus ``OTHER`` for a strategy
    dblect does not model (a custom macro-defined one). The set is closed so the
    key-need classification branches over every member explicitly. A strategy left
    unset is ``None`` rather than a member here: the absence is meaningful (it resolves
    to an adapter default), so the caller keeps it distinct from a named strategy.
    """

    APPEND = "append"
    MERGE = "merge"
    DELETE_INSERT = "delete+insert"
    INSERT_OVERWRITE = "insert_overwrite"
    MICROBATCH = "microbatch"
    OTHER = "other"

    @classmethod
    def from_raw(cls, raw: str | None) -> IncrementalStrategy | None:
        """The strategy a raw config value names, or ``None`` when unset.

        A recognized name (case-folded) maps to its member; an unrecognized non-empty
        name maps to ``OTHER`` (a custom strategy); ``None`` or an empty/whitespace value
        is the unset default, returned as ``None``.
        """
        if raw is None or not raw.strip():
            return None
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return cls.OTHER


class _KeyNeed(Enum):
    """Whether an incremental strategy needs a ``unique_key`` to stay idempotent."""

    NEEDS_KEY = auto()
    """Append-style: a rerun duplicates rows unless a key reconciles them."""
    RECONCILES = auto()
    """Replaces rows (a partition, the whole table, or a time batch), so it is idempotent
    without a key."""
    UNKNOWN = auto()
    """A custom strategy whose idempotency dblect cannot judge; stay silent."""


def _key_need(strategy: IncrementalStrategy) -> _KeyNeed:
    """How `strategy` reconciles a rerun's rows, decided over the closed strategy set.

    ``merge`` and ``delete+insert`` both take a ``unique_key`` to match or delete on;
    without one dbt falls back to a plain append, so they sit with ``append`` as
    needing a key. ``insert_overwrite`` and ``microbatch`` overwrite rather than append.
    """
    match strategy:
        case (
            IncrementalStrategy.APPEND
            | IncrementalStrategy.MERGE
            | IncrementalStrategy.DELETE_INSERT
        ):
            return _KeyNeed.NEEDS_KEY
        case IncrementalStrategy.INSERT_OVERWRITE | IncrementalStrategy.MICROBATCH:
            return _KeyNeed.RECONCILES
        case IncrementalStrategy.OTHER:
            return _KeyNeed.UNKNOWN
    assert_never(strategy)


def _strategy_clause(strategy: IncrementalStrategy | None) -> str:
    """The phrase naming the strategy in the finding message. An unset strategy reads as
    the adapter default rather than a named one."""
    if strategy is None:
        return "no incremental_strategy is set (the adapter default appends)"
    return f"the '{strategy.value}' strategy appends without a key to reconcile on"


def incremental_findings(node: Node) -> tuple[Finding, ...]:
    """Flag `node` when it is an incremental model that appends without a ``unique_key``.

    Returns one model-scoped finding (line 0) when the materialization is
    ``incremental``, no ``unique_key`` is declared, and the strategy is append-style (or
    unset). Silent otherwise: a declared key reconciles rows, an overwriting strategy is
    idempotent without one, a custom strategy's idempotency is unknown, and a
    non-incremental model never appends.
    """
    config = node.config
    if config is None:
        return ()
    if Materialization.from_raw(config.materialized) is not Materialization.INCREMENTAL:
        return ()
    if config.unique_key:
        return ()
    strategy = IncrementalStrategy.from_raw(config.incremental_strategy)
    # An unset strategy resolves to an append-style default on every adapter, so it needs
    # a key the same way the named append-style strategies do.
    need = _KeyNeed.NEEDS_KEY if strategy is None else _key_need(strategy)
    match need:
        case _KeyNeed.RECONCILES | _KeyNeed.UNKNOWN:
            return ()
        case _KeyNeed.NEEDS_KEY:
            message = (
                f"incremental model declares no unique_key and {_strategy_clause(strategy)}, "
                "so each run appends its rows: a rerun, backfill, or late-arriving data "
                "duplicates them. Declare a unique_key, or use an idempotent strategy "
                "(insert_overwrite, microbatch)."
            )
            return (
                Finding(
                    kind=FindingKind.INCREMENTAL_MISSING_UNIQUE_KEY,
                    message=message,
                    sql_snippet="",
                    line_start=0,
                    line_end=0,
                ),
            )
    assert_never(need)
