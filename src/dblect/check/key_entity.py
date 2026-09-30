"""Which entity a key column identifies, inferred from declared keys and foreign keys.

An order id and a customer id are both unique integers, so a join that equates them
is a clean one-to-one join no test catches. The project already says which columns
are keys and which reference which, and that is enough to tell the two apart:

* A declared single-column key starts an entity, unless the SQL already makes the
  column unique. Grouping on a date makes it unique without making it an identifier,
  and a key carried through from upstream belongs to the upstream entity.
* A column that copies another (a rename, a cast, a CTE or model boundary, a UNION
  whose arms all carry one entity) is in the same entity.
* A foreign key puts the child column in the parent's entity.

A column is in an entity only if one of these reaches it, so most columns have none.
Two columns in different entities have no declared relationship; that is a strong
hint the join is wrong, and exactly what an untested 1:1 table also looks like.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.model import Annotation, Fact
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceRef, UnionConfluence
from dblect.lineage.properties.uniqueness import CandidateKeySet
from dblect.lineage.property import resolved_column_ref
from dblect.types.bridge import ForeignKeyEdge


@dataclass(frozen=True, slots=True)
class KeyEntity:
    """An entity, named by the declared keys that start it (usually one)."""

    keys: frozenset[ColumnRef]


def entity_keys(
    declared: Mapping[SourceRef, tuple[Fact[CandidateKeySet, SourceRef], ...]],
    inferred: Mapping[SourceRef, Annotation[CandidateKeySet]],
) -> frozenset[ColumnRef]:
    """The declared keys that start an entity: single-column, unconditional, and not
    already implied by the keys the SQL alone gives the relation."""
    out: set[ColumnRef] = set()
    for scope, facts in declared.items():
        sql_keys = inferred.get(scope)
        for fact in facts:
            if fact.condition is not None:
                continue
            for key in fact.value.keys:
                if len(key) != 1:
                    continue
                if sql_keys is not None and _implies(sql_keys.value, key):
                    continue
                out.add(ColumnRef(scope, next(iter(key)).lower()))
    return frozenset(out)


def _implies(keys: CandidateKeySet, key: frozenset[str]) -> bool:
    """Whether ``keys`` makes ``key`` unique: some known key is a subset of it."""
    return keys.is_bottom or any(k <= key for k in keys.keys)


def key_entities(
    graph: ColumnLineageGraph,
    keys: Iterable[ColumnRef],
    foreign_keys: Iterable[ForeignKeyEdge],
) -> Callable[[ColumnRef], KeyEntity | None]:
    """The entity of each column, or ``None`` for a column no key reaches."""
    classes = _UnionFind()
    for fk in foreign_keys:
        classes.union(_folded(fk.child), _folded(fk.parent))
    confluences: list[tuple[ColumnRef, tuple[ColumnRef, ...]]] = []
    for subject, derivation in graph.expressions.items():
        if isinstance(derivation, UnionConfluence):
            confluences.append((subject, derivation.arm_refs))
        elif (source := _copied_column(derivation)) is not None:
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
    classes: _UnionFind, keys: frozenset[ColumnRef]
) -> Callable[[ColumnRef], KeyEntity | None]:
    by_root: dict[ColumnRef, set[ColumnRef]] = {}
    for key in keys:
        by_root.setdefault(classes.find(key), set()).add(key)
    entities = {root: KeyEntity(frozenset(members)) for root, members in by_root.items()}
    return lambda ref: entities.get(classes.find(ref))


def _copied_column(derivation: Expr) -> ColumnRef | None:
    """The column ``derivation`` copies unchanged, through renames, parentheses, and
    casts; ``None`` for anything computed."""
    node = derivation
    while isinstance(node, (exp.Alias, exp.Paren, exp.Cast)):
        node = node.this
    return resolved_column_ref(node) if isinstance(node, exp.Column) else None


def _folded(ref: ColumnRef) -> ColumnRef:
    """``ref`` with its column case-folded, as the lineage keys columns. A contract's
    foreign key keeps the spelling it was declared with."""
    return ColumnRef(ref.source, ref.column.lower())


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[ColumnRef, ColumnRef] = {}

    def find(self, ref: ColumnRef) -> ColumnRef:
        root = ref
        while (up := self._parent.get(root, root)) != root:
            root = up
        while ref != root:
            self._parent[ref], ref = root, self._parent.get(ref, root)
        return root

    def union(self, a: ColumnRef, b: ColumnRef) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb
