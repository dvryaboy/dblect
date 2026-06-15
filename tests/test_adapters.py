"""The adapter profile and its registry.

The strategy normalization, the override resolution, and the conservative fallback
carry the real behavior. The per-adapter data is pinned only where it has bitten
(the incremental dedup default), parametrized rather than restated per adapter.
Resolving any builtin (snowflake, bigquery, ...) below also exercises the
registry's auto-discovery, since none of these import an adapter module directly.
"""

from __future__ import annotations

import pytest

from dblect.adapters import (
    AdapterProfile,
    IncrementalStrategy,
    UnvalidatedAdapterError,
    profile_for_adapter,
    register,
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
    assert IncrementalStrategy.parse(raw) is None


# --- effective_strategy and the per-adapter defaults -------------------------


@pytest.mark.parametrize(
    ("adapter", "declared", "expected"),
    [
        ("snowflake", "append", IncrementalStrategy.APPEND),  # a declared known strategy wins
        ("snowflake", "my_custom", None),  # a custom strategy is not assumed to dedup
        ("snowflake", None, IncrementalStrategy.MERGE),  # unset -> the adapter default, which is...
        ("bigquery", None, IncrementalStrategy.MERGE),
        (
            "postgres",
            None,
            IncrementalStrategy.DELETE_INSERT,
        ),  # ...delete+insert on pg/redshift, not merge
        ("redshift", None, IncrementalStrategy.DELETE_INSERT),
        ("duckdb", None, None),  # duckdb's dedup default is left unset
    ],
)
def test_effective_strategy(
    adapter: str, declared: str | None, expected: IncrementalStrategy | None
) -> None:
    assert profile_for_adapter(adapter).effective_strategy(declared) is expected


# --- profile_for_adapter -----------------------------------------------------


@pytest.mark.parametrize(
    ("adapter", "key_enforced"),
    [("duckdb", True), ("postgres", True), ("snowflake", False), ("bigquery", False)],
)
def test_key_enforcement_per_adapter(adapter: str, key_enforced: bool) -> None:
    # PRIMARY KEY / UNIQUE is advisory on the cloud warehouses, enforced on duckdb and Postgres.
    assert profile_for_adapter(adapter).key_enforced is key_enforced


def test_adapter_lookup_is_case_and_whitespace_insensitive() -> None:
    assert profile_for_adapter("  SnowFlake ") is profile_for_adapter("snowflake")


def test_unknown_adapter_is_conservative_never_raises() -> None:
    """An adapter no module registered gets advisory keys, NOT NULL enforced, no
    dedup default, and its name carried through as the dialect guess."""
    profile = profile_for_adapter("exotic_warehouse")
    assert (profile.key_enforced, profile.not_null_enforced, profile.validated) == (
        False,
        True,
        False,
    )
    assert profile.default_incremental_strategy is None
    assert profile.sqlglot_dialect == "exotic_warehouse"


# --- resolve_profile: the parsing-validation gate and override coherence -----


def test_validated_adapter_resolves_without_override() -> None:
    profile = resolve_profile(adapter_type="duckdb", explicit_dialect=None)
    assert profile.validated is True
    assert profile.sqlglot_dialect == "duckdb"


def test_unvalidated_adapter_without_override_raises() -> None:
    with pytest.raises(UnvalidatedAdapterError) as exc_info:
        resolve_profile(adapter_type="snowflake", explicit_dialect=None)
    assert exc_info.value.adapter_type == "snowflake"


@pytest.mark.parametrize(
    ("adapter", "override", "dialect", "validated", "default"),
    [
        # The override names the whole target: snowflake's grammar AND its semantics,
        # never a hybrid of one adapter's grammar with another's enforcement.
        ("acme_warehouse", "snowflake", "snowflake", False, IncrementalStrategy.MERGE),
        ("snowflake", "duckdb", "duckdb", True, None),  # validated follows the override target
        ("snowflake", "exotic", "exotic", False, None),  # an unknown override stays conservative
    ],
)
def test_override_names_the_whole_target(
    adapter: str,
    override: str,
    dialect: str,
    validated: bool,
    default: IncrementalStrategy | None,
) -> None:
    profile = resolve_profile(adapter_type=adapter, explicit_dialect=override)
    assert profile.sqlglot_dialect == dialect
    assert profile.validated is validated
    assert profile.default_incremental_strategy is default


# --- the registry is the extension point -------------------------------------


def test_register_makes_a_new_adapter_resolvable() -> None:
    """Supporting a warehouse is registering a profile, with no core map to edit;
    the builtins register themselves this same way."""
    profile = register(
        AdapterProfile(
            adapter_type="widget_warehouse",
            sqlglot_dialect="duckdb",
            validated=False,
            not_null_enforced=True,
            key_enforced=True,
            default_incremental_strategy=IncrementalStrategy.MERGE,
        )
    )
    assert profile_for_adapter("widget_warehouse") is profile
