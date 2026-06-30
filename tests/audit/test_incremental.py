"""The incremental-config check: flag an incremental model whose write appends duplicates.

These pin the decision over the manifest ``config`` and the resolved adapter profile (no
SQL parse): the materialization, whether a ``unique_key`` is declared, and the effective
incremental strategy. The strategy space is closed, so it is enumerated per value crossed
with key present/absent rather than sampled (see the case tables below for the per-strategy
verdicts and why each holds).
"""

from __future__ import annotations

import pytest

from dblect.adapters import (
    DEDUP_STRATEGIES,
    AdapterProfile,
    IncrementalStrategy,
    profile_for_adapter,
)
from dblect.audit.incremental import incremental_findings
from dblect.manifest import Materialization, ModelConfig, Node, ResourceType
from dblect.sql import FindingKind

# Profiles chosen for their default_incremental_strategy: Snowflake defaults to merge,
# Postgres to delete+insert, DuckDB carries no validated default, and an adapter no module
# registered falls back to the conservative profile, also with no default.
_SNOWFLAKE = profile_for_adapter("snowflake")  # default merge
_POSTGRES = profile_for_adapter("postgres")  # default delete+insert
_DUCKDB = profile_for_adapter("duckdb")  # default None
_UNKNOWN = profile_for_adapter("nonesuch-warehouse")  # conservative, default None


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


def _fires(strategy: str | None, *, has_key: bool, profile: AdapterProfile = _SNOWFLAKE) -> bool:
    keys = ("id",) if has_key else ()
    model = _model(incremental_strategy=strategy, unique_key=keys)
    return bool(incremental_findings(model, profile))


# The full explicit strategy space (every IncrementalStrategy member) crossed with key
# present/absent, with the verdict the adapter substrate dictates. `append` fires whether or
# not a key is declared, because its write ignores `unique_key`. `merge`/`delete+insert` fire
# only without a key (with one they deduplicate). `insert_overwrite`/`microbatch` overwrite,
# so they never fire. The profile's default is irrelevant when the strategy is explicit.
_EXPLICIT_CASES: tuple[tuple[str, bool, bool], ...] = (
    ("append", False, True),
    ("append", True, True),
    ("merge", False, True),
    ("merge", True, False),
    ("delete+insert", False, True),
    ("delete+insert", True, False),
    ("insert_overwrite", False, False),
    ("insert_overwrite", True, False),
    ("microbatch", False, False),
    ("microbatch", True, False),
)


@pytest.mark.parametrize(("strategy", "has_key", "should_fire"), _EXPLICIT_CASES)
def test_explicit_strategy_verdict(strategy: str, has_key: bool, should_fire: bool) -> None:
    assert _fires(strategy, has_key=has_key) is should_fire


def test_append_with_key_still_fires() -> None:
    # The case the firewall exists to catch and the easy one to miss: dbt's append inserts
    # unconditionally and never reads unique_key, so a declared key does not reconcile the
    # rerun's rows. A short-circuit on "a key is present" would wrongly silence this.
    [finding] = incremental_findings(
        _model(incremental_strategy="append", unique_key=("id",)), _SNOWFLAKE
    )
    assert finding.kind is FindingKind.INCREMENTAL_MISSING_UNIQUE_KEY
    # Model-scoped: the config block is stripped from compiled SQL, so there is no line to
    # anchor to, and the snippet is empty.
    assert finding.line_start == 0
    assert finding.line_end == 0
    assert finding.sql_snippet == ""


def test_unique_key_flips_verdict_exactly_for_dedup_strategies() -> None:
    # The (strategy, key) reasoning is tied to the adapter substrate's DEDUP_STRATEGIES: a
    # declared unique_key changes the verdict for exactly the strategies whose write
    # deduplicates on it, and for no others. Pins the audit to that shared set so the two
    # cannot drift (a strategy added to one but not the other).
    flips = {
        s
        for s in IncrementalStrategy
        if _fires(s.value, has_key=False) != _fires(s.value, has_key=True)
    }
    assert flips == set(DEDUP_STRATEGIES)


def test_custom_strategy_is_silent() -> None:
    # A macro-defined strategy dblect does not model: effective_strategy resolves it to
    # None, an unknown idempotency, so the firewall stays silent rather than guess. Silent
    # with or without a declared key.
    assert IncrementalStrategy.parse("my_team_upsert") is None
    assert incremental_findings(_model(incremental_strategy="my_team_upsert"), _SNOWFLAKE) == ()
    assert (
        incremental_findings(
            _model(incremental_strategy="my_team_upsert", unique_key=("id",)), _SNOWFLAKE
        )
        == ()
    )


# An unset strategy runs under the adapter's default, so the verdict is the default's
# verdict: a merge/delete+insert default with no key fires, and a profile with no known
# default stays silent rather than assume an append default.
_UNSET_CASES = (
    pytest.param(_SNOWFLAKE, False, True, id="merge-default-no-key-fires"),
    pytest.param(_SNOWFLAKE, True, False, id="merge-default-with-key-silent"),
    pytest.param(_POSTGRES, False, True, id="delete-insert-default-no-key-fires"),
    pytest.param(_DUCKDB, False, False, id="no-known-default-stays-silent"),
    pytest.param(_DUCKDB, True, False, id="no-known-default-with-key-silent"),
    pytest.param(_UNKNOWN, False, False, id="conservative-default-stays-silent"),
)


@pytest.mark.parametrize(("profile", "has_key", "should_fire"), _UNSET_CASES)
def test_unset_strategy_follows_adapter_default(
    profile: AdapterProfile, has_key: bool, should_fire: bool
) -> None:
    assert _fires(None, has_key=has_key, profile=profile) is should_fire


# Every materialization that is not ``incremental``: none of them append, so none fire.
_NON_INCREMENTAL: tuple[str, ...] = tuple(
    m.value for m in Materialization if m is not Materialization.INCREMENTAL
)


@pytest.mark.parametrize("materialized", _NON_INCREMENTAL)
def test_non_incremental_materialization_is_silent(materialized: str) -> None:
    # Even with no key and an append-style strategy set, a model that is not incremental
    # never appends, so nothing fires.
    assert incremental_findings(_model(materialized=materialized), _SNOWFLAKE) == ()


def test_absent_materialized_is_silent() -> None:
    assert incremental_findings(_model(materialized=None), _SNOWFLAKE) == ()


def test_no_config_block_is_silent() -> None:
    assert incremental_findings(_model(config=None), _SNOWFLAKE) == ()


def test_materialized_is_case_insensitive() -> None:
    # dbt lower-cases these, but the classifier folds case so a manifest that carried the
    # raw casing is still read as incremental.
    [finding] = incremental_findings(_model(materialized="INCREMENTAL"), _SNOWFLAKE)
    assert finding.kind is FindingKind.INCREMENTAL_MISSING_UNIQUE_KEY


def test_strategy_is_case_insensitive() -> None:
    # effective_strategy folds case via IncrementalStrategy.parse, so raw casing resolves
    # the same: insert_overwrite stays silent, merge (no key) fires.
    assert incremental_findings(_model(incremental_strategy="Insert_Overwrite"), _SNOWFLAKE) == ()
    assert bool(incremental_findings(_model(incremental_strategy="MERGE"), _SNOWFLAKE))


def test_append_message_notes_the_key_has_no_effect() -> None:
    # When append fires despite a declared key, the message must not tell the user to add a
    # key (they have one); it names append and the remedy of a reconciling strategy.
    [finding] = incremental_findings(
        _model(incremental_strategy="append", unique_key=("id",)), _SNOWFLAKE
    )
    assert "append" in finding.message
    assert "no effect" in finding.message
    assert "merge" in finding.message


def test_dedup_without_key_message_says_declare_a_key() -> None:
    [finding] = incremental_findings(_model(incremental_strategy="merge"), _SNOWFLAKE)
    assert "merge" in finding.message
    assert "unique_key" in finding.message
