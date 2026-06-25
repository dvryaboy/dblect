"""The adapter profile and its registry.

These exercise logic, not the registered data: string normalization, the
``effective_strategy`` branches, the validation gate, and the rule that an
override resolves to the override target's whole profile. The per-adapter facts
themselves (which warehouse enforces keys, what each defaults to) are domain data
that a test could only re-state; their behavioral consequences are pinned where
they act, in ``test_uniqueness_facts`` (enforcement) and ``test_config_facts``
(the dedup default grounding a key).
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
    validated_adapters,
)
from dblect.sql import PORTABLE_NON_DETERMINISTIC_BUILTINS

# --- IncrementalStrategy.parse: normalization and the custom -> None branch ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("merge", IncrementalStrategy.MERGE),  # exact match
        ("MERGE", IncrementalStrategy.MERGE),  # case-folded
        ("  delete+insert  ", IncrementalStrategy.DELETE_INSERT),  # whitespace-stripped
    ],
)
def test_parse_normalizes_known_strategies(raw: str, expected: IncrementalStrategy) -> None:
    assert IncrementalStrategy.parse(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "my_custom_strategy"])
def test_parse_unset_or_custom_is_none(raw: str | None) -> None:
    assert IncrementalStrategy.parse(raw) is None


# --- AdapterProfile.effective_strategy: the three branches --------------------


def test_effective_strategy_branches() -> None:
    # A constructed profile, so this tests the method's logic, not any adapter's
    # registered default. The default here is a dedup strategy precisely so the
    # custom-strategy case can show it does NOT fall back to it.
    profile = AdapterProfile(
        adapter_type="x",
        sqlglot_dialect="duckdb",
        validated=False,
        not_null_enforced=True,
        key_enforced=False,
        default_incremental_strategy=IncrementalStrategy.MERGE,
        non_deterministic_builtins=frozenset(),
    )
    assert profile.effective_strategy("append") is IncrementalStrategy.APPEND  # declared wins
    assert profile.effective_strategy("custom_macro") is None  # custom is not assumed to dedup
    assert (
        profile.effective_strategy(None) is profile.default_incremental_strategy
    )  # unset -> default


# --- profile_for_adapter ------------------------------------------------------


def test_adapter_lookup_is_case_and_whitespace_insensitive() -> None:
    assert profile_for_adapter("  SnowFlake ") is profile_for_adapter("snowflake")


def test_unknown_adapter_is_conservative_never_raises() -> None:
    """An adapter no module registered must not over-claim: advisory keys, no dedup
    default, unvalidated, with the name carried through as the dialect guess."""
    profile = profile_for_adapter("exotic_warehouse")
    assert profile.key_enforced is False
    assert profile.default_incremental_strategy is None
    assert profile.validated is False
    assert profile.sqlglot_dialect == "exotic_warehouse"


# --- resolve_profile: the validation gate and override coherence --------------


def test_validated_adapter_resolves_to_its_profile() -> None:
    assert resolve_profile(adapter_type="duckdb", explicit_dialect=None) == profile_for_adapter(
        "duckdb"
    )


def test_bigquery_is_validated_and_resolves_without_override() -> None:
    # bigquery is a validated adapter: it resolves without a --dialect override and
    # carries the bigquery sqlglot dialect and the (advisory) unenforced-key facet.
    profile = resolve_profile(adapter_type="bigquery", explicit_dialect=None)
    assert profile.validated is True
    assert "bigquery" in validated_adapters()
    assert profile.sqlglot_dialect == "bigquery"
    assert profile.key_enforced is False


def test_unvalidated_adapter_without_override_raises() -> None:
    with pytest.raises(UnvalidatedAdapterError) as exc_info:
        resolve_profile(adapter_type="snowflake", explicit_dialect=None)
    assert exc_info.value.adapter_type == "snowflake"


def test_override_resolves_to_the_override_targets_whole_profile() -> None:
    # The contract that kills the hybrid: an override names the target wholesale,
    # so the resolved profile IS the override adapter's profile (grammar AND
    # semantics), never the manifest adapter's semantics under another grammar.
    # Using an unvalidated manifest adapter also shows the override bypasses the gate.
    assert resolve_profile(
        adapter_type="acme_warehouse", explicit_dialect="snowflake"
    ) == profile_for_adapter("snowflake")


# --- the registry is the extension point --------------------------------------


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
            non_deterministic_builtins=frozenset(),
        )
    )
    assert profile_for_adapter("widget_warehouse") is profile


# --- per-adapter non-determinism knowledge ------------------------------------


def test_unknown_adapter_gets_the_portable_non_determinism_baseline() -> None:
    # An adapter dblect has no module for falls back to the portable baseline, never
    # an empty or guessed set, so portable hazards still fire on a best-effort target.
    assert (
        profile_for_adapter("exotic_warehouse").non_deterministic_builtins
        == PORTABLE_NON_DETERMINISTIC_BUILTINS
    )


def test_duckdb_extends_the_portable_non_determinism_baseline() -> None:
    # The validated adapter strictly extends the baseline: it carries the whole set
    # (baseline plus its own), so the detector reads one value and unions nothing.
    builtins = profile_for_adapter("duckdb").non_deterministic_builtins
    assert builtins > PORTABLE_NON_DETERMINISTIC_BUILTINS
    assert "txid_current" in builtins  # a DuckDB builtin absent from the baseline
    assert "txid_current" not in PORTABLE_NON_DETERMINISTIC_BUILTINS
