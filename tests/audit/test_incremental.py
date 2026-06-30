"""The incremental-config check: flag an incremental model that appends without a key.

These pin the decision over the manifest ``config`` alone (no SQL parse): the
materialization, the incremental strategy, and whether a ``unique_key`` is declared. The
strategy space is closed, so it is enumerated per value rather than sampled, and the
common axes (key present vs absent, every named strategy, the unset default, a custom
one, the non-incremental materializations) each earn a case.
"""

from __future__ import annotations

import pytest

from dblect.audit.incremental import IncrementalStrategy, incremental_findings
from dblect.manifest import Materialization, ModelConfig, Node, ResourceType
from dblect.sql import FindingKind

# Append-style strategies need a key to stay idempotent; the unset default (``None``)
# resolves to one of these on every adapter, so it sits in the same bucket.
_NEEDS_KEY: tuple[str | None, ...] = (None, "append", "merge", "delete+insert")
# These reconcile a rerun's rows without a key (overwrite a partition / the whole table /
# a time batch), so an incremental model is idempotent under them with no key declared.
_RECONCILES: tuple[str, ...] = ("insert_overwrite", "microbatch")
# Every materialization that is not ``incremental``: none of them append, so none fire.
_NON_INCREMENTAL: tuple[str, ...] = tuple(
    m.value for m in Materialization if m is not Materialization.INCREMENTAL
)


def _model(
    *,
    materialized: str | None = "incremental",
    incremental_strategy: str | None = None,
    unique_key: tuple[str, ...] = (),
    config: ModelConfig | None | bool = True,
) -> Node:
    resolved = (
        None
        if config is None
        else ModelConfig(
            materialized=materialized,
            incremental_strategy=incremental_strategy,
            unique_key=unique_key,
        )
    )
    return Node(
        unique_id="model.shop.fct",
        name="fct",
        resource_type=ResourceType.MODEL,
        fqn=("shop", "fct"),
        package_name="shop",
        schema="analytics",
        raw_code="select 1",
        compiled_code="select 1",
        original_file_path="models/fct.sql",
        columns={},
        config=resolved,
    )


@pytest.mark.parametrize("strategy", _NEEDS_KEY)
def test_append_style_without_key_fires(strategy: str | None) -> None:
    [finding] = incremental_findings(_model(incremental_strategy=strategy))
    assert finding.kind is FindingKind.INCREMENTAL_MISSING_UNIQUE_KEY
    # Model-scoped: the config block is stripped from compiled SQL, so there is no line to
    # anchor to, and the snippet is empty.
    assert finding.line_start == 0
    assert finding.line_end == 0
    assert finding.sql_snippet == ""
    assert "unique_key" in finding.message


@pytest.mark.parametrize("strategy", _NEEDS_KEY)
def test_append_style_with_key_is_silent(strategy: str | None) -> None:
    # A declared unique_key reconciles a rerun's rows, so the append concern is discharged.
    assert incremental_findings(_model(incremental_strategy=strategy, unique_key=("id",))) == ()


@pytest.mark.parametrize("strategy", _RECONCILES)
def test_reconciling_strategy_without_key_is_silent(strategy: str) -> None:
    assert incremental_findings(_model(incremental_strategy=strategy)) == ()


def test_custom_strategy_is_silent() -> None:
    # A macro-defined strategy dblect does not model: its idempotency is unknown, so the
    # firewall stays silent rather than guess.
    assert IncrementalStrategy.from_raw("my_team_upsert") is IncrementalStrategy.OTHER
    assert incremental_findings(_model(incremental_strategy="my_team_upsert")) == ()


@pytest.mark.parametrize("materialized", _NON_INCREMENTAL)
def test_non_incremental_materialization_is_silent(materialized: str) -> None:
    # Even with no key and an append-style strategy left set, a model that is not
    # incremental never appends, so nothing fires.
    assert incremental_findings(_model(materialized=materialized)) == ()


def test_absent_materialized_is_silent() -> None:
    assert incremental_findings(_model(materialized=None)) == ()


def test_no_config_block_is_silent() -> None:
    assert incremental_findings(_model(config=None)) == ()


def test_materialized_is_case_insensitive() -> None:
    # dbt lower-cases these, but the classifier folds case so a manifest that carried the
    # raw casing is still read as incremental.
    [finding] = incremental_findings(_model(materialized="INCREMENTAL"))
    assert finding.kind is FindingKind.INCREMENTAL_MISSING_UNIQUE_KEY


def test_strategy_is_case_insensitive() -> None:
    assert IncrementalStrategy.from_raw("Insert_Overwrite") is IncrementalStrategy.INSERT_OVERWRITE
    assert incremental_findings(_model(incremental_strategy="Insert_Overwrite")) == ()


def test_message_names_the_strategy_when_set() -> None:
    [finding] = incremental_findings(_model(incremental_strategy="merge"))
    assert "merge" in finding.message


def test_message_notes_the_default_when_strategy_unset() -> None:
    [finding] = incremental_findings(_model(incremental_strategy=None))
    assert "default" in finding.message
