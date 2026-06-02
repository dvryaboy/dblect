"""The annotation store the propagator fills, and the registry that orders
properties and hands each one a typed view of the others' results.

``AnnotationStore`` accumulates every node's annotation across properties during
one run. ``PropertyRegistry`` fixes the three things the single-pass walk rests
on: a name maps to exactly one property, every ``depends_on`` edge resolves to a
registered property's *minted* ref by identity, and the dependency graph is
acyclic so the walk needs no fixpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from dblect.lineage.facts.model import Annotation
from dblect.lineage.facts.property import DepContext, Property, PropertyRef
from dblect.lineage.graph import ColumnRef, SourceRef

_Scope = ColumnRef | SourceRef


def _empty_index() -> dict[str, dict[_Scope, Annotation[Any]]]:
    return {}


@dataclass(slots=True)
class AnnotationStore:
    """Annotations accumulated across properties during one propagation run, keyed
    by property name and scope. A single store holds both column- and
    relation-scoped properties; the (name, scope) key keeps them separate without
    the store knowing a property's scope kind. Write-once per (name, scope) in a
    correct run: the propagator visits each node once per property."""

    _by_property: dict[str, dict[_Scope, Annotation[Any]]] = field(default_factory=_empty_index)

    def record(self, name: str, scope: _Scope, annotation: Annotation[Any]) -> None:
        self._by_property.setdefault(name, {})[scope] = annotation

    def get(self, name: str, scope: _Scope) -> Annotation[Any] | None:
        return self._by_property.get(name, {}).get(scope)


@dataclass(frozen=True, slots=True)
class _StoreDepContext:
    """The read-only projection of an AnnotationStore a property's transfers see.

    ``ref`` is the minted handle of a registered property (the registry checked
    that at assembly), so recovering the dependency's value type ``K2`` from it is
    sound; the store erases to ``Annotation[Any]`` only internally."""

    _store: AnnotationStore

    def annotation(self, ref: PropertyRef[Any, Any], scope: Any) -> Annotation[Any] | None:
        return self._store.get(ref.name, scope)


@dataclass(frozen=True, slots=True)
class PropertyRegistry:
    properties: tuple[Property[Any, Any], ...]

    def evaluation_order(self) -> tuple[Property[Any, Any], ...]:
        """Topological order over ``depends_on``, dependencies first.

        Raises ``ValueError`` on a duplicate name, on an edge whose ref is not the
        minted ref of a registered property (an identity check, not a name match,
        so a forged or stale handle fails here), or on a cycle. Independent
        properties keep their input order, so the result is deterministic.
        """
        by_name: dict[str, Property[Any, Any]] = {}
        for prop in self.properties:
            if prop.name in by_name:
                raise ValueError(f"duplicate property name {prop.name!r} in registry")
            by_name[prop.name] = prop

        # Resolve each edge to the exact registered property by ref identity.
        registered_refs = {id(prop.ref): prop for prop in self.properties}
        deps: dict[str, list[Property[Any, Any]]] = {}
        for prop in self.properties:
            resolved: list[Property[Any, Any]] = []
            for edge in prop.depends_on:
                target = registered_refs.get(id(edge))
                if target is None:
                    raise ValueError(
                        f"property {prop.name!r} depends_on {edge.name!r}, which is not a "
                        "registered property's ref (identity check)"
                    )
                resolved.append(target)
            deps[prop.name] = resolved

        # Kahn's algorithm with input order as the tiebreak, so the output is
        # stable and a leftover node signals a cycle.
        ordered: list[Property[Any, Any]] = []
        placed: set[str] = set()
        remaining = list(self.properties)
        while remaining:
            progressed = False
            still: list[Property[Any, Any]] = []
            for prop in remaining:
                if all(dep.name in placed for dep in deps[prop.name]):
                    ordered.append(prop)
                    placed.add(prop.name)
                    progressed = True
                else:
                    still.append(prop)
            remaining = still
            if not progressed:
                names = ", ".join(sorted(p.name for p in remaining))
                raise ValueError(f"depends_on cycle among: {names}")
        return tuple(ordered)

    def dep_context(self, store: AnnotationStore) -> DepContext:
        """A read-only view of the annotations computed so far, keyed by
        (name, scope)."""
        return cast("DepContext", _StoreDepContext(store))
