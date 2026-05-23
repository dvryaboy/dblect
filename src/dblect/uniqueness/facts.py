"""Uniqueness facts derived from a dbt manifest and model SQL.

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
* **Structural proof / propagation** from the model's SQL: ``select distinct``,
  top-level ``GROUP BY``, ``UNION`` (distinct), and pass-throughs of ref'd
  model keys via projection, JOIN, and CTE all surface as
  ``UniquenessSource.STRUCTURAL_PROOF`` or ``PROPAGATED`` facts. The analysis
  reads ``compiled_code`` so it sees SQL after macro expansion.

Each fact carries provenance so reviewers can see *why* dblect believes a key
is unique. Propagated facts also carry ``derived_from``, a chain of parent
facts the key inherits from. Reasoning over uniqueness is opportunistic: when
we have a fact, downstream detectors can use it; when we don't, they stay
silent.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from sqlglot import Expr

from dblect.manifest import ConstraintSpec, ConstraintType, Manifest, Node, ResourceType
from dblect.sql import SQLParseError, parse_sql


class UniquenessSource(StrEnum):
    DBT_UNIQUE_TEST = "dbt_unique_test"
    DBT_UNIQUE_COMBINATION_TEST = "dbt_unique_combination_test"
    NATIVE_CONSTRAINT = "native_constraint"
    STRUCTURAL_PROOF = "structural_proof"
    PROPAGATED = "propagated"


@dataclass(frozen=True, slots=True)
class UniquenessFact:
    """A single uniqueness claim about a set of columns on one model.

    Most facts originate at a single declaration or structural shape and have
    an empty ``derived_from``. Facts produced by SQL-level propagation (a CTE
    that pass-throughs a ref'd model's keys, a JOIN that preserves the left
    side's keys) carry the parent fact(s) they inherit from in
    ``derived_from``; chains let reviewers trace why the audit believes a
    derived key is unique.
    """

    model_unique_id: str
    columns: frozenset[str]
    source: UniquenessSource
    detail: str | None = None
    derived_from: tuple[UniquenessFact, ...] = ()


def facts_from_manifest(
    manifest: Manifest,
    *,
    dialect: str | None = "duckdb",
    parsed: Mapping[str, Expr] | None = None,
) -> Mapping[str, tuple[UniquenessFact, ...]]:
    """All known uniqueness facts for `manifest`, grouped by node unique_id.

    Combines two layers:

    * **Declaration ingestion**: dbt unique tests, dbt-utils composite-key
      tests, dbt 1.5+ native constraints (model-level and column-level).
      Tests on sources ground facts on the source uid; downstream models
      that ``ref`` the source pick them up by name.
    * **Propagation through SQL**: top-level ``DISTINCT`` and ``GROUP BY``
      shapes, ``UNION`` (distinct), and pass-throughs of ref'd model/source
      keys via projection, JOIN, and CTE. Models are processed in
      topological order, so a downstream model's propagation sees every
      upstream's combined declared + propagated facts.

    `parsed` lets callers that already have a per-model `Expr` (e.g. the
    audit walker) skip a redundant parse pass. When omitted, this function
    parses each model's `analysis_sql` itself, swallowing parse errors.

    Facts for a given node are de-duplicated on column set; when the same
    set is grounded by multiple sources the strongest one wins (native
    constraint > unique test > combination test > structural proof >
    propagated). Nodes with no known facts are absent from the mapping.
    """
    by_node: defaultdict[str, list[UniquenessFact]] = defaultdict(list)
    for fact in _all_declaration_facts(manifest):
        by_node[fact.model_unique_id].append(fact)
    trees = parsed if parsed is not None else _parse_models(manifest, dialect=dialect)
    name_to_uid = _build_name_to_uid(manifest)

    from dblect.uniqueness.propagation import facts_from_tree

    model_uids = set(manifest.models)
    for uid in manifest.dag.topological_order():
        if uid not in model_uids:
            continue
        tree = trees.get(uid)
        if tree is None:
            continue
        current: Mapping[str, tuple[UniquenessFact, ...]] = {
            k: tuple(v) for k, v in by_node.items()
        }
        for fact in facts_from_tree(
            uid, tree, model_facts=current, model_name_to_uid=name_to_uid
        ):
            by_node[fact.model_unique_id].append(fact)
    return {uid: _dedupe(facts) for uid, facts in by_node.items()}


def _build_name_to_uid(manifest: Manifest) -> Mapping[str, str]:
    """Map of relation name → unique_id covering models and sources.

    Sources are keyed by ``identifier or name`` since that's the rightmost
    name in compiled SQL when dbt resolves ``{{ source(...) }}``. Models win
    on collisions: a developer writing ``{{ ref('x') }}`` expects the model
    named ``x``, not a source that happens to share the name.
    """
    name_to_uid: dict[str, str] = {}
    for uid, src in manifest.sources.items():
        name_to_uid.setdefault(src.identifier or src.name, uid)
    for uid, model in manifest.models.items():
        name_to_uid[model.name] = uid
    return name_to_uid


_SOURCE_RANK: Mapping[UniquenessSource, int] = {
    UniquenessSource.NATIVE_CONSTRAINT: 0,
    UniquenessSource.DBT_UNIQUE_TEST: 1,
    UniquenessSource.DBT_UNIQUE_COMBINATION_TEST: 2,
    UniquenessSource.STRUCTURAL_PROOF: 3,
    UniquenessSource.PROPAGATED: 4,
}


def _dedupe(facts: Iterable[UniquenessFact]) -> tuple[UniquenessFact, ...]:
    """Collapse facts on the same column set, keeping the strongest source.

    A model whose ``customer_id`` is both a declared unique test and a
    propagated key (from a CTE pass-through of an upstream model) yields a
    single fact, sourced as ``DBT_UNIQUE_TEST``. Tie order between same-rank
    sources follows insertion order: first-seen wins, which keeps the
    declaration ahead of the structural shape inferred from the same SQL.
    """
    keep: dict[frozenset[str], UniquenessFact] = {}
    for f in facts:
        existing = keep.get(f.columns)
        if existing is None or _SOURCE_RANK[f.source] < _SOURCE_RANK[existing.source]:
            keep[f.columns] = f
    return tuple(keep.values())


def facts_from_declarations(manifest: Manifest) -> tuple[UniquenessFact, ...]:
    """Just the declaration-derived facts (tests + native constraints)."""
    return tuple(_all_declaration_facts(manifest))


def _all_declaration_facts(manifest: Manifest) -> Iterable[UniquenessFact]:
    yield from _facts_from_tests(manifest.nodes.values())
    yield from _facts_from_native_constraints(manifest.nodes.values())


def _parse_models(manifest: Manifest, *, dialect: str | None) -> Mapping[str, Expr]:
    """Parse each model's analysis SQL; skip models with no SQL or parse errors."""
    out: dict[str, Expr] = {}
    for model in manifest.models.values():
        sql = model.analysis_sql
        if sql is None:
            continue
        try:
            out[model.unique_id] = parse_sql(sql, dialect=dialect)
        except SQLParseError:
            continue
    return out


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
        target = _test_target_node(node)
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


_TARGET_PREFIXES: tuple[str, ...] = ("model.", "source.")


def _test_target_node(node: Node) -> str | None:
    """The unique_id a generic test is attached to, or None if undeterminable.

    Prefer ``attached_node`` (the modern shape); fall back to the first
    eligible entry in ``depends_on`` for older manifest versions where
    ``attached_node`` isn't populated. Models and sources both accept generic
    tests and both feed downstream model SQL, so both ground facts; seeds and
    snapshots are out of scope for now.
    """
    if node.attached_node and node.attached_node.startswith(_TARGET_PREFIXES):
        return node.attached_node
    for dep in sorted(node.depends_on):
        if dep.startswith(_TARGET_PREFIXES):
            return dep
    return None
