"""Parsing model ``config`` into the typed ``ModelConfig`` view.

dbt's ``unique_key`` is heterogeneous (a single string or a list of strings), so
the normalization to a tuple of column names is the part with real logic. These
exercise it through the public parse boundary (``Manifest.from_raw`` over the
vendored jaffle manifest with one model's config overridden), so the test
survives any refactor of the private parse helpers and uses the same strict
schema dbt itself emits.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from dblect.manifest import Manifest, ModelConfig


@pytest.fixture(scope="module")
def jaffle_raw(jaffle_manifest_path: Path) -> Mapping[str, Any]:
    raw: dict[str, Any] = json.loads(jaffle_manifest_path.read_text())
    return raw


def _a_model_uid(raw: Mapping[str, Any]) -> str:
    return next(uid for uid, n in raw["nodes"].items() if n.get("resource_type") == "model")


def _with_config(raw: Mapping[str, Any], uid: str, **overrides: object) -> Manifest:
    mutated = copy.deepcopy(dict(raw))
    mutated["nodes"][uid]["config"].update(overrides)
    return Manifest.from_raw(mutated)


def _config(raw: Mapping[str, Any], uid: str, **overrides: object) -> ModelConfig:
    config = _with_config(raw, uid, **overrides).nodes[uid].config
    assert config is not None  # every parsed model carries a config block
    return config


def test_reads_materialized_and_strategy(jaffle_raw: Mapping[str, Any]) -> None:
    uid = _a_model_uid(jaffle_raw)
    cfg = _config(
        jaffle_raw, uid, materialized="incremental", incremental_strategy="merge", unique_key="id"
    )
    assert cfg.materialized == "incremental"
    assert cfg.incremental_strategy == "merge"
    assert cfg.unique_key == ("id",)


def test_unique_key_string_normalizes_to_single_element_tuple(
    jaffle_raw: Mapping[str, Any],
) -> None:
    uid = _a_model_uid(jaffle_raw)
    assert _config(jaffle_raw, uid, unique_key="event_id").unique_key == ("event_id",)


def test_unique_key_list_normalizes_to_tuple(jaffle_raw: Mapping[str, Any]) -> None:
    uid = _a_model_uid(jaffle_raw)
    cfg = _config(jaffle_raw, uid, unique_key=["event_id", "event_date"])
    assert cfg.unique_key == ("event_id", "event_date")


def test_unique_key_absent_is_empty_tuple(jaffle_raw: Mapping[str, Any]) -> None:
    # jaffle's models declare no unique_key, so the unmutated config grounds to the
    # empty tuple, the "no key" shape the config discoverer reads as silence.
    uid = _a_model_uid(jaffle_raw)
    config = Manifest.from_raw(copy.deepcopy(dict(jaffle_raw))).nodes[uid].config
    assert config is not None
    assert config.unique_key == ()
