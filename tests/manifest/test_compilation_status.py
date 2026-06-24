"""A node's compilation status, read at manifest load.

The analysis assumes ``compiled_code`` faithfully represents the model. That
assumption breaks two ways. A compile run that did not reach the warehouse leaves
some nodes with empty or absent ``compiled_code`` even though their template is
non-trivial, and the manifest's own ``compiled`` flag can mark a node as never
compiled. Both must surface as a coverage miss rather than be analysed as if the
model were empty.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from dblect.manifest import CompilationStatus, Manifest


@pytest.fixture(scope="module")
def jaffle_raw(jaffle_manifest_path: Path) -> Mapping[str, Any]:
    return json.loads(jaffle_manifest_path.read_text())


def _a_model_uid(raw: Mapping[str, Any]) -> str:
    return next(uid for uid, n in raw["nodes"].items() if n.get("resource_type") == "model")


def _with_node(raw: Mapping[str, Any], uid: str, **overrides: object) -> Manifest:
    mutated = copy.deepcopy(dict(raw))
    mutated["nodes"][uid].update(overrides)
    return Manifest.from_raw(mutated)


def test_jaffle_models_are_compiled(jaffle_raw: Mapping[str, Any]) -> None:
    manifest = Manifest.from_raw(copy.deepcopy(dict(jaffle_raw)))
    for node in manifest.models.values():
        assert node.compilation_status is CompilationStatus.COMPILED


def test_empty_compiled_code_with_nontrivial_raw_is_stale_or_absent(
    jaffle_raw: Mapping[str, Any],
) -> None:
    uid = _a_model_uid(jaffle_raw)
    manifest = _with_node(jaffle_raw, uid, compiled_code="", compiled=True)
    assert manifest.nodes[uid].compilation_status is CompilationStatus.STALE_OR_ABSENT


def test_manifest_marks_node_not_compiled(jaffle_raw: Mapping[str, Any]) -> None:
    uid = _a_model_uid(jaffle_raw)
    # dbt's own flag says the node was not compiled, even if a code field lingers.
    manifest = _with_node(jaffle_raw, uid, compiled=False, compiled_code=None)
    assert manifest.nodes[uid].compilation_status is CompilationStatus.NOT_COMPILED


def test_whitespace_only_raw_with_empty_compiled_is_compiled(
    jaffle_raw: Mapping[str, Any],
) -> None:
    # A model whose template is trivially empty has nothing to compile; an empty
    # compiled body is faithful there, not a missed compilation.
    uid = _a_model_uid(jaffle_raw)
    manifest = _with_node(jaffle_raw, uid, raw_code="   \n  ", compiled_code="", compiled=True)
    assert manifest.nodes[uid].compilation_status is CompilationStatus.COMPILED


def test_python_model_is_not_a_stale_compile_miss(jaffle_raw: Mapping[str, Any]) -> None:
    # A Python model is not SQL-analysable; an empty SQL ``compiled_code`` is not the
    # non-hermetic-compile gap, so it must not be surfaced as a stale/absent coverage
    # miss that tells the user to run `dbt compile` against a warehouse.
    uid = _a_model_uid(jaffle_raw)
    manifest = _with_node(jaffle_raw, uid, language="python", compiled_code="", compiled=True)
    assert manifest.nodes[uid].compilation_status is CompilationStatus.COMPILED


def test_status_present_for_a_node_with_no_compiled_flag() -> None:
    # A manifest shape with no `compiled` flag and present compiled_code reads as
    # compiled (the flag defaults absent on older schemas).
    from dblect.manifest import Node, ResourceType

    node = Node(
        unique_id="model.shop.m",
        name="m",
        resource_type=ResourceType.MODEL,
        fqn=("shop", "m"),
        package_name="shop",
        schema="analytics",
        raw_code="select 1",
        compiled_code="select 1",
        original_file_path="models/m.sql",
        columns={},
    )
    assert node.compilation_status is CompilationStatus.COMPILED
