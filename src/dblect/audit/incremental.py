"""Flag dbt incremental models that silently append duplicate rows.

An incremental model whose write does not reconcile a rerun's rows appends every run's
output to the existing table. A backfill, a rerun, or late-arriving data then lands the
same logical row twice, a duplicate the model's own SQL never reveals because each run is
internally correct. dbt's docs call this out; this raises it at audit time from the
manifest config alone, with no SQL parse needed.

The decision is over the node's resolved ``config`` and the run's adapter ``profile``: the
materialization, whether a ``unique_key`` is declared, and the *effective* incremental
strategy. The strategy is effective rather than declared because a model that leaves
``incremental_strategy`` unset runs under the adapter's default, which is adapter-specific
(``merge`` on Snowflake and BigQuery, ``delete+insert`` on Postgres and Redshift, and a
value dblect has not validated on some others). :meth:`AdapterProfile.effective_strategy`
owns that resolution, the same substrate the uniqueness property's config-key discoverer
reasons over, so the audit and the property agree on what a given model's write does.
:func:`_appends_on_rerun` then classifies that strategy against the declared key.

The finding is model-scoped (line 0): the concern is the model's configuration as a whole,
and the ``config()`` block is stripped from the compiled SQL the audit parses, so there is
no SQL line to anchor it to.
"""

from __future__ import annotations

from typing import assert_never

from dblect.adapters import AdapterProfile, IncrementalStrategy
from dblect.manifest import Materialization, Node
from dblect.sql import Finding, FindingKind


def _appends_on_rerun(strategy: IncrementalStrategy | None, *, has_key: bool) -> bool:
    """True when an incremental write under `strategy` appends a rerun's rows rather than
    reconciling them, decided over the closed strategy set (plus ``None`` for a strategy
    dblect cannot resolve to a known behavior).

    ``append`` ignores ``unique_key`` and inserts unconditionally, so it duplicates with or
    without a key. ``merge`` and ``delete+insert`` reconcile on a key, but without one dbt
    falls back to appending, so they duplicate exactly when no key is declared.
    ``insert_overwrite`` and ``microbatch`` overwrite rather than append, so they are
    idempotent without a key. ``None`` (a custom strategy, or an adapter default dblect does
    not model) has unknown idempotency, so it stays silent.
    """
    match strategy:
        case None:
            return False
        case IncrementalStrategy.APPEND:
            return True
        case IncrementalStrategy.MERGE | IncrementalStrategy.DELETE_INSERT:
            return not has_key
        case IncrementalStrategy.INSERT_OVERWRITE | IncrementalStrategy.MICROBATCH:
            return False
    assert_never(strategy)


def _message(strategy: IncrementalStrategy, *, has_key: bool) -> str:
    """The finding text for a firing model. Only ``append`` and the dedup strategies reach
    here (the strategies for which :func:`_appends_on_rerun` returns ``True``), and the fix
    differs: ``append`` ignores a key, so the remedy is a different strategy, while a dedup
    strategy with no key just needs one declared."""
    tail = "so each run appends its rows: a rerun, backfill, or late-arriving data duplicates them."
    if strategy is IncrementalStrategy.APPEND:
        ignored = " (the declared unique_key has no effect under append)" if has_key else ""
        return (
            f"incremental model uses the 'append' strategy, which inserts every run's rows "
            f"and ignores unique_key{ignored}, {tail} Use merge or delete+insert with a "
            "unique_key, or an idempotent strategy (insert_overwrite, microbatch)."
        )
    return (
        f"incremental model declares no unique_key and uses the '{strategy.value}' strategy, "
        f"which deduplicates only on a unique_key, so dbt falls back to appending, {tail} "
        "Declare a unique_key, or use an idempotent strategy (insert_overwrite, microbatch)."
    )


def incremental_findings(node: Node, profile: AdapterProfile) -> tuple[Finding, ...]:
    """Flag `node` when it is an incremental model whose write appends duplicate rows.

    Returns one model-scoped finding (line 0) when the materialization is ``incremental``
    and the effective strategy (resolved against `profile` so an unset strategy reads as the
    adapter's default) appends a rerun's rows without reconciling them. Silent otherwise: a
    dedup strategy with a declared ``unique_key`` reconciles rows, an overwriting strategy is
    idempotent without one, and a strategy dblect cannot resolve is left alone.
    """
    config = node.config
    if config is None:
        return ()
    if Materialization.from_raw(config.materialized) is not Materialization.INCREMENTAL:
        return ()
    has_key = bool(config.unique_key)
    strategy = profile.effective_strategy(config.incremental_strategy)
    if not _appends_on_rerun(strategy, has_key=has_key):
        return ()
    # _appends_on_rerun is False for None, so a firing strategy is always a concrete member.
    assert strategy is not None
    return (
        Finding(
            kind=FindingKind.INCREMENTAL_MISSING_UNIQUE_KEY,
            message=_message(strategy, has_key=has_key),
            sql_snippet="",
            line_start=0,
            line_end=0,
        ),
    )
