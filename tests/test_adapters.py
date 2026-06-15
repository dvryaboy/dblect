"""The adapter profile: one coherent target per dbt adapter.

These pin the contract the rest of dblect reads through: that resolving a target
yields a single value whose SQL grammar and runtime semantics come from the same
adapter, so an override can never leave the two disagreeing. The strategy
normalization and the per-adapter defaults are the parts with real logic, so they
are exercised directly.
"""

from __future__ import annotations

import pytest

from dblect.adapters import (
    DEDUP_STRATEGIES,
    IncrementalStrategy,
    UnvalidatedAdapterError,
    profile_for_adapter,
    resolve_profile,
)

# --- IncrementalStrategy.parse -----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("merge", IncrementalStrategy.MERGE),
        ("MERGE", IncrementalStrategy.MERGE),
        ("  delete+insert  ", IncrementalStrategy.DELETE_INSERT),
        ("append", IncrementalStrategy.APPEND),
        ("insert_overwrite", IncrementalStrategy.INSERT_OVERWRITE),
        ("microbatch", IncrementalStrategy.MICROBATCH),
    ],
)
def test_parse_recognizes_dbt_builtins(raw: str, expected: IncrementalStrategy) -> None:
    assert IncrementalStrategy.parse(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "my_custom_strategy", "upsert"])
def test_parse_unset_or_custom_is_none(raw: str | None) -> None:
    """An unset or project-defined strategy is not a known builtin, so it carries
    no dedup guarantee dblect can assume."""
    assert IncrementalStrategy.parse(raw) is None


def test_dedup_strategies_are_merge_and_delete_insert() -> None:
    assert {IncrementalStrategy.MERGE, IncrementalStrategy.DELETE_INSERT} == DEDUP_STRATEGIES


# --- AdapterProfile.effective_strategy ---------------------------------------


def test_declared_strategy_wins_over_default() -> None:
    snowflake = profile_for_adapter("snowflake")  # default merge
    assert snowflake.effective_strategy("append") is IncrementalStrategy.APPEND


def test_custom_declared_strategy_does_not_fall_back_to_default() -> None:
    """A model that explicitly chose a custom strategy gets no dedup claim, even on
    an adapter whose default dedups: the explicit choice is not the default."""
    snowflake = profile_for_adapter("snowflake")  # default merge
    assert snowflake.effective_strategy("my_custom_strategy") is None


def test_unset_strategy_uses_the_adapter_default() -> None:
    assert profile_for_adapter("snowflake").effective_strategy(None) is IncrementalStrategy.MERGE
    assert (
        profile_for_adapter("postgres").effective_strategy(None)
        is IncrementalStrategy.DELETE_INSERT
    )
    assert profile_for_adapter("duckdb").effective_strategy(None) is None


# --- profile_for_adapter -----------------------------------------------------


def test_known_adapter_enforcement_flags() -> None:
    # PRIMARY KEY / UNIQUE: advisory on the cloud warehouses, enforced on duckdb
    # and Postgres. NOT NULL: enforced everywhere.
    assert profile_for_adapter("duckdb").key_enforced is True
    assert profile_for_adapter("postgres").key_enforced is True
    assert profile_for_adapter("snowflake").key_enforced is False
    assert profile_for_adapter("bigquery").key_enforced is False
    assert all(
        profile_for_adapter(a).not_null_enforced
        for a in ("duckdb", "snowflake", "bigquery", "redshift", "postgres")
    )


def test_adapter_lookup_is_case_and_whitespace_insensitive() -> None:
    assert profile_for_adapter("  SnowFlake ").default_incremental_strategy is (
        IncrementalStrategy.MERGE
    )


def test_unknown_adapter_is_conservative_never_raises() -> None:
    """The semantics lookup tolerates any adapter: advisory keys, NOT NULL
    enforced, no dedup default, and the name carried through as the dialect guess."""
    profile = profile_for_adapter("exotic_warehouse")
    assert profile.key_enforced is False
    assert profile.not_null_enforced is True
    assert profile.default_incremental_strategy is None
    assert profile.validated is False
    assert profile.sqlglot_dialect == "exotic_warehouse"


# --- resolve_profile: the parsing-validation gate and override coherence -----


def test_validated_adapter_resolves_without_override() -> None:
    profile = resolve_profile(adapter_type="duckdb", explicit_dialect=None)
    assert profile.adapter_type == "duckdb"
    assert profile.sqlglot_dialect == "duckdb"
    assert profile.validated is True


def test_unvalidated_adapter_without_override_raises() -> None:
    with pytest.raises(UnvalidatedAdapterError) as exc_info:
        resolve_profile(adapter_type="snowflake", explicit_dialect=None)
    assert exc_info.value.adapter_type == "snowflake"


def test_override_selects_the_whole_target_so_grammar_and_semantics_agree() -> None:
    """The core guarantee: an override names the target wholesale. Forcing snowflake
    onto an unknown adapter yields snowflake's grammar AND snowflake's runtime
    semantics, never a hybrid of one adapter's grammar with another's semantics."""
    profile = resolve_profile(adapter_type="acme_warehouse", explicit_dialect="snowflake")
    assert profile.sqlglot_dialect == "snowflake"
    assert profile.default_incremental_strategy is IncrementalStrategy.MERGE
    assert profile.key_enforced is False  # snowflake's, not the unknown adapter's


def test_override_to_validated_target_is_validated() -> None:
    profile = resolve_profile(adapter_type="snowflake", explicit_dialect="duckdb")
    assert profile.sqlglot_dialect == "duckdb"
    assert profile.validated is True


def test_override_to_unknown_dialect_stays_conservative() -> None:
    profile = resolve_profile(adapter_type="snowflake", explicit_dialect="exotic")
    assert profile.sqlglot_dialect == "exotic"
    assert profile.validated is False
    assert profile.default_incremental_strategy is None
