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

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.manifest import ConstraintSpec, ConstraintType, Manifest, Node, ResourceType
from dblect.sql import ParsedSQL, SQLParseError
from dblect.sql import _sqlglot as sg


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


def facts_from_manifest(
    manifest: Manifest, *, dialect: str | None = "duckdb"
) -> Mapping[str, tuple[UniquenessFact, ...]]:
    """All known uniqueness facts for `manifest`, grouped by model unique_id.

    Combines two layers:

    * **Declaration ingestion**: dbt unique tests, dbt-utils composite-key
      tests, dbt 1.5+ native constraints (model-level and column-level).
    * **Structural proof from SQL**: ``SELECT DISTINCT`` and ``GROUP BY``
      shapes in each model's ``raw_code``.

    Models with no known facts are absent from the mapping; callers should
    treat missing keys as "no facts known" rather than assuming uniqueness
    one way or the other.
    """
    by_model: defaultdict[str, list[UniquenessFact]] = defaultdict(list)
    for fact in _all_declaration_facts(manifest):
        by_model[fact.model_unique_id].append(fact)
    for fact in _all_structural_facts(manifest, dialect=dialect):
        by_model[fact.model_unique_id].append(fact)
    return {uid: tuple(facts) for uid, facts in by_model.items()}


def facts_from_declarations(manifest: Manifest) -> tuple[UniquenessFact, ...]:
    """Just the declaration-derived facts (tests + native constraints)."""
    return tuple(_all_declaration_facts(manifest))


def facts_from_sql(model_unique_id: str, parsed: ParsedSQL) -> tuple[UniquenessFact, ...]:
    """Uniqueness facts provable from a model's SQL alone (DISTINCT, GROUP BY).

    Only the top-level ``SELECT`` (the one that produces the model's output
    rows) is considered. Inner ``SELECT`` shapes inside CTEs or subqueries
    don't prove anything about the outer model's output. Set operations
    (``UNION`` and friends) are out of scope for this first cut.
    """
    sel = _top_level_select(parsed.tree)
    if sel is None:
        return ()
    out: list[UniquenessFact] = []
    out.extend(_facts_from_distinct(model_unique_id, sel))
    out.extend(_facts_from_group_by(model_unique_id, sel))
    return tuple(out)


def _all_declaration_facts(manifest: Manifest) -> Iterable[UniquenessFact]:
    yield from _facts_from_tests(manifest.nodes.values())
    yield from _facts_from_native_constraints(manifest.nodes.values())


def _all_structural_facts(
    manifest: Manifest, *, dialect: str | None
) -> Iterable[UniquenessFact]:
    for model in manifest.models.values():
        if model.raw_code is None:
            continue
        try:
            parsed = ParsedSQL.parse(model.raw_code, dialect=dialect)
        except SQLParseError:
            continue
        yield from facts_from_sql(model.unique_id, parsed)


def _facts_from_tests(nodes: Iterable[Node]) -> Iterable[UniquenessFact]:
    for node in nodes:
        tm = node.test_metadata
        if tm is None:
            continue
        # Disabled tests don't run, so they prove nothing. A `where` filter
        # makes the assertion conditional ("unique within rows matching X"),
        # which doesn't translate to the unconditional UniquenessFact shape
        # downstream detectors assume; skip rather than over-claim.
        if not tm.enabled or tm.where is not None:
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
                    detail=f"model-level {c.type.value}",
                )
        # Column-level constraints: the column they're attached to is implicit.
        for col_name, col in node.columns.items():
            for c in col.constraints:
                if c.type in _UNIQUENESS_CONSTRAINT_TYPES:
                    yield UniquenessFact(
                        model_unique_id=node.unique_id,
                        columns=frozenset({col_name}),
                        source=UniquenessSource.NATIVE_CONSTRAINT,
                        detail=f"column-level {c.type.value} on {col_name}",
                    )


_UNIQUENESS_CONSTRAINT_TYPES: frozenset[ConstraintType] = frozenset(
    {ConstraintType.PRIMARY_KEY, ConstraintType.UNIQUE}
)


def _uniqueness_columns(c: ConstraintSpec) -> frozenset[str] | None:
    if c.type not in _UNIQUENESS_CONSTRAINT_TYPES:
        return None
    if not c.columns:
        return None
    return frozenset(c.columns)


def _top_level_select(tree: Expr) -> exp.Select | None:
    """The ``SELECT`` whose output is the model's output, or ``None``.

    A bare ``Select`` is returned as-is. A ``WITH`` clause's body
    (``tree.this``) is unwrapped. Set operations (``UNION`` and friends) are
    out of scope: their output uniqueness is shape-dependent and we don't
    reason about them yet.
    """
    if isinstance(tree, exp.Select):
        return tree
    if isinstance(tree, exp.With):
        body = tree.this
        if isinstance(body, exp.Select):
            return body
    return None


def _facts_from_distinct(model_unique_id: str, sel: exp.Select) -> Iterable[UniquenessFact]:
    """``SELECT DISTINCT a, b FROM ...`` proves the output is unique on ``(a, b)``."""
    if sel.args.get("distinct") is None:
        return
    columns = _project_output_columns(sel)
    if not columns:
        return
    yield UniquenessFact(
        model_unique_id=model_unique_id,
        columns=frozenset(columns),
        source=UniquenessSource.STRUCTURAL_PROOF,
        detail="top-level SELECT DISTINCT",
    )


def _facts_from_group_by(model_unique_id: str, sel: exp.Select) -> Iterable[UniquenessFact]:
    """``SELECT cols, ... FROM ... GROUP BY cols`` proves the output is unique on those cols.

    Only emits a fact when every GROUP BY target resolves to a bare column
    that's also in the SELECT projection. That is, the uniqueness claim can be
    expressed in terms of named output columns. ``GROUP BY 1, 2`` (positional)
    and GROUP BY over computed expressions are skipped to keep the rule
    conservative.
    """
    group = sg.group_of(sel)
    if group is None or not group.expressions:
        return
    group_cols: list[str] = []
    for g in group.expressions:
        if not isinstance(g, exp.Column):
            return
        group_cols.append(sg.column_name(g))
    projected = _project_output_columns(sel)
    if not projected:
        return
    projected_set = set(projected)
    if not all(name in projected_set for name in group_cols):
        return
    yield UniquenessFact(
        model_unique_id=model_unique_id,
        columns=frozenset(group_cols),
        source=UniquenessSource.STRUCTURAL_PROOF,
        detail="top-level GROUP BY",
    )


def _project_output_columns(sel: exp.Select) -> list[str]:
    """Output column names from `sel`'s projection list.

    Only counts projections that resolve to a named output column: bare
    ``exp.Column`` references and ``exp.Alias`` wrappers. Computed expressions
    without an alias get no name and are skipped, since they can't participate in
    a column-name uniqueness claim.
    """
    names: list[str] = []
    for proj in sel.expressions:
        if isinstance(proj, exp.Alias):
            names.append(proj.alias_or_name)
        elif isinstance(proj, exp.Column):
            names.append(sg.column_name(proj))
    return names


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
