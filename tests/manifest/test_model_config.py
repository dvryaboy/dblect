"""Parsing model ``config`` into the typed ``ModelConfig`` view.

dbt's ``unique_key`` is heterogeneous (a single string, a list of strings, or
absent), so the normalization to a tuple of column names is the part with real
logic and is pinned here at the parse boundary. The parser reads config by
``getattr``, so lightweight stubs stand in for the version-specific parsed node.
"""

from __future__ import annotations

from types import SimpleNamespace

from dblect.manifest import ModelConfig
from dblect.manifest.parse import _model_config_from_parsed


def _parsed(**config: object) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(**config))


def test_reads_materialized_and_strategy() -> None:
    cfg = _model_config_from_parsed(
        _parsed(materialized="incremental", incremental_strategy="merge", unique_key="id")
    )
    assert cfg == ModelConfig(
        materialized="incremental", incremental_strategy="merge", unique_key=("id",)
    )


def test_unique_key_string_normalizes_to_single_element_tuple() -> None:
    cfg = _model_config_from_parsed(_parsed(unique_key="event_id"))
    assert cfg.unique_key == ("event_id",)


def test_unique_key_list_normalizes_to_tuple() -> None:
    cfg = _model_config_from_parsed(_parsed(unique_key=["event_id", "event_date"]))
    assert cfg.unique_key == ("event_id", "event_date")


def test_unique_key_absent_is_empty_tuple() -> None:
    cfg = _model_config_from_parsed(_parsed(materialized="view"))
    assert cfg.unique_key == ()


def test_unique_key_non_string_entries_are_dropped() -> None:
    # A malformed list (a null, a nested list) is not a usable column name; the
    # parser keeps the string entries and drops the rest rather than failing.
    cfg = _model_config_from_parsed(_parsed(unique_key=["event_id", None, ["x"]]))
    assert cfg.unique_key == ("event_id",)


def test_missing_config_block_is_none() -> None:
    assert _model_config_from_parsed(SimpleNamespace()) is None
