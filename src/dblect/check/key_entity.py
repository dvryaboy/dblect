"""Which entity a key column identifies, inferred from declared keys and foreign keys.

An order id and a customer id are both unique integers, so a join that equates them
is a clean one-to-one join no test catches. The project already says which columns
are keys and which reference which, and that is enough to tell the two apart:

* A declared single-column key starts an entity when the key column is a base
  relation's column or an unchanged copy of one. The entity is anchored at that base
  column, so a staging model's key on ``id AS customer_id`` and the source's key on
  ``id`` are one entity. A computed key (an expression, a union, a dedup) says
  nothing about which entity it identifies, so it starts none.
* A column that copies another (a rename, a cast, a CTE or model boundary, a UNION
  whose arms all carry one entity) is in the same entity.
* A foreign key puts the child column in the parent's entity.

A column is in an entity only if one of these reaches it, so most columns have none.
Two columns in different entities have no declared relationship; that is a strong
hint the join is wrong, and exactly what an untested 1:1 table also looks like. Two
natural keys with no relationships test between them (two date-keyed sources) look
the same and are flagged too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import assert_never

from dblect.lineage.facts.model import CompileValue, Declared, Fact, NativeConstraint, Provenance
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceRef, UnionConfluence
from dblect.lineage.properties.uniqueness import CandidateKeySet
from dblect.lineage.property import copied_column, value_origin
from dblect.lineage.union_find import UnionFind
from dblect.sql._sqlglot import stored_column_name
from dblect.types.bridge import ForeignKeyEdge


@dataclass(frozen=True, slots=True)
class KeyEntity:
    """An entity, named by the declared keys that start it (usually one)."""

    keys: frozenset[ColumnRef]


def entity_keys(
    graph: ColumnLineageGraph,
    declared: Mapping[SourceRef, tuple[Fact[CandidateKeySet, SourceRef], ...]],
) -> frozenset[ColumnRef]:
    """The base columns whose declared single-column, unconditional keys start an
    entity."""
    out: set[ColumnRef] = set()
    for scope, facts in declared.items():
        for fact in facts:
            if fact.condition is not None or not _states_identity(fact.provenance):
                continue
            for key in fact.value.keys:
                if len(key) != 1:
                    continue
                origin = value_origin(graph, ColumnRef(scope, stored_column_name(next(iter(key)))))
                if origin is not None:
                    out.add(origin)
    return frozenset(out)


def _states_identity(provenance: Provenance) -> bool:
    """Whether a key from this source says what the column identifies. An incremental
    model's ``unique_key`` is a merge key in config, not a statement about the entity."""
    match provenance:
        case Declared() | NativeConstraint():
            return True
        case CompileValue():
            return False
    assert_never(provenance)


def key_entities(
    graph: ColumnLineageGraph,
    keys: Iterable[ColumnRef],
    foreign_keys: Iterable[ForeignKeyEdge],
) -> Callable[[ColumnRef], KeyEntity | None]:
    """The entity of each column, or ``None`` for a column no key reaches."""
    classes = UnionFind[ColumnRef]()
    for fk in foreign_keys:
        classes.union(fk.child, fk.parent)
    confluences: list[tuple[ColumnRef, tuple[ColumnRef, ...]]] = []
    for subject, derivation in graph.expressions.items():
        if isinstance(derivation, UnionConfluence):
            confluences.append((subject, derivation.arm_refs))
        elif (source := copied_column(derivation)) is not None:
            classes.union(subject, source)
    key_set = frozenset(keys)
    # A UNION output joins its arms' entity only when every arm has that one entity,
    # and an arm may itself be a UNION output, so repeat until nothing merges.
    changed = True
    while changed:
        changed = False
        entity_of = _entities(classes, key_set)
        for subject, arms in confluences:
            arm_entities = {entity_of(arm) for arm in arms}
            if len(arm_entities) != 1 or None in arm_entities:
                continue
            if classes.find(subject) != classes.find(arms[0]):
                classes.union(subject, arms[0])
                changed = True
    return _entities(classes, key_set)


def _entities(
    classes: UnionFind[ColumnRef], keys: frozenset[ColumnRef]
) -> Callable[[ColumnRef], KeyEntity | None]:
    by_root: dict[ColumnRef, set[ColumnRef]] = {}
    for key in keys:
        by_root.setdefault(classes.find(key), set()).add(key)
    entities = {root: KeyEntity(frozenset(members)) for root, members in by_root.items()}
    return lambda ref: entities.get(classes.find(ref))
