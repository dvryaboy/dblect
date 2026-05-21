"""Uniqueness facts derived from a dbt manifest and (later) model SQL.

A **uniqueness fact** is a claim that a set of columns is jointly unique on a
specific model. Facts come from multiple sources:

* **dbt unique tests** (single column): a generic ``unique`` test attached to
  a column produces a single-column fact.
* **dbt-utils unique_combination_of_columns** (composite key): the
  ``unique_combination_of_columns`` test's ``combination_of_columns`` kwarg
  becomes a multi-column fact.
* **Native dbt constraints** (dbt 1.5+): model-level ``primary_key`` /
  ``unique`` constraints carry a column list; column-level constraints carry
  the implicit single-column key.
* **Structural proof** from the model's SQL (separate layer, future): e.g.
  ``select distinct cols`` or ``select cols, ... group by cols`` proves the
  output is unique on ``cols``.

Each fact carries provenance so reviewers can see *why* dblect believes a key
is unique. Reasoning over uniqueness is opportunistic: when we have a fact,
downstream detectors can use it; when we don't, they stay silent.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from dblect.manifest import ConstraintSpec, Manifest, Node, ResourceType


class UniquenessSource(StrEnum):
    DBT_UNIQUE_TEST = "dbt_unique_test"
    DBT_UNIQUE_COMBINATION_TEST = "dbt_unique_combination_test"
    NATIVE_CONSTRAINT = "native_constraint"
    STRUCTURAL_PROOF = "structural_proof"


@dataclass(frozen=True, slots=True)
class UniquenessFact:
    """A single uniqueness claim about a set of columns on one model."""

    model_unique_id: str
    columns: frozenset[str]
    source: UniquenessSource
    detail: str | None = None


def facts_from_manifest(manifest: Manifest) -> Mapping[str, tuple[UniquenessFact, ...]]:
    """Every declaration-derived uniqueness fact, grouped by model unique_id.

    Reads dbt test nodes, dbt-utils composite-key tests, and dbt 1.5+ native
    constraints (model-level and column-level). Returns a mapping keyed by
    model unique_id; models without any known facts are absent from the
    mapping (callers should treat missing keys as "no facts known").
    """
    by_model: defaultdict[str, list[UniquenessFact]] = defaultdict(list)
    for fact in _facts_from_tests(manifest.nodes.values()):
        by_model[fact.model_unique_id].append(fact)
    for fact in _facts_from_native_constraints(manifest.nodes.values()):
        by_model[fact.model_unique_id].append(fact)
    return {uid: tuple(facts) for uid, facts in by_model.items()}


def _facts_from_tests(nodes: Iterable[Node]) -> Iterable[UniquenessFact]:
    for node in nodes:
        tm = node.test_metadata
        if tm is None:
            continue
        target = _test_target_model(node)
        if target is None:
            continue
        if tm.name == "unique":
            col = tm.kwargs.get("column_name")
            if isinstance(col, str) and col:
                yield UniquenessFact(
                    model_unique_id=target,
                    columns=frozenset({col}),
                    source=UniquenessSource.DBT_UNIQUE_TEST,
                    detail=node.name,
                )
        elif tm.name.endswith("unique_combination_of_columns"):
            raw_combo: object = tm.kwargs.get("combination_of_columns")
            if not isinstance(raw_combo, list):
                continue
            raw_list = cast("list[Any]", raw_combo)
            combo: list[str] = [c for c in raw_list if isinstance(c, str)]
            if combo and len(combo) == len(raw_list):
                yield UniquenessFact(
                    model_unique_id=target,
                    columns=frozenset(combo),
                    source=UniquenessSource.DBT_UNIQUE_COMBINATION_TEST,
                    detail=node.name,
                )


def _facts_from_native_constraints(nodes: Iterable[Node]) -> Iterable[UniquenessFact]:
    for node in nodes:
        if node.resource_type is not ResourceType.MODEL:
            continue
        # Model-level constraints: columns are explicit.
        for c in node.constraints:
            cols = _uniqueness_columns(c)
            if cols is not None:
                yield UniquenessFact(
                    model_unique_id=node.unique_id,
                    columns=cols,
                    source=UniquenessSource.NATIVE_CONSTRAINT,
                    detail=f"model-level {c.type}",
                )
        # Column-level constraints: the column they're attached to is implicit.
        for col_name, col in node.columns.items():
            for c in col.constraints:
                if _is_uniqueness_constraint(c.type):
                    yield UniquenessFact(
                        model_unique_id=node.unique_id,
                        columns=frozenset({col_name}),
                        source=UniquenessSource.NATIVE_CONSTRAINT,
                        detail=f"column-level {c.type} on {col_name}",
                    )


_UNIQUENESS_CONSTRAINT_TYPES: frozenset[str] = frozenset({"primary_key", "unique"})


def _is_uniqueness_constraint(type_str: str) -> bool:
    return type_str.lower() in _UNIQUENESS_CONSTRAINT_TYPES


def _uniqueness_columns(c: ConstraintSpec) -> frozenset[str] | None:
    if not _is_uniqueness_constraint(c.type):
        return None
    if not c.columns:
        return None
    return frozenset(c.columns)


def _test_target_model(node: Node) -> str | None:
    """The model unique_id a generic test is attached to, or None if undeterminable.

    Prefer ``attached_node`` (the modern shape); fall back to the first model
    in ``depends_on`` for older manifest versions where ``attached_node``
    isn't populated. Sources, seeds, and snapshots also accept generic tests
    but uniqueness reasoning on those is a different story; we restrict to
    models here.
    """
    if node.attached_node and node.attached_node.startswith("model."):
        return node.attached_node
    for dep in sorted(node.depends_on):
        if dep.startswith("model."):
            return dep
    return None
