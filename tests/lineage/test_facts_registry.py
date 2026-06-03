"""AnnotationStore and PropertyRegistry.

These pin the assembly-time guarantees the single-pass walk rests on: a name
maps to one property, depends_on edges are checked by ref *identity* (so a
forged or stale handle fails rather than mistyping a read), the graph is
acyclic, and evaluation order is a topological sort. The DepContext reads an
earlier property's annotations out of the store and a silent dependency reads
as None.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation
from dblect.lineage.facts.property import (
    Property,
    PropertyRef,
    column_property,
)
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef

_BOOL: Lattice[bool] = Lattice(
    meet=lambda a, b: a and b, join=lambda a, b: a or b, top=False, bottom=True
)
_COL = ColumnRef(SourceRef(SourceKind.MODEL, "model.shop.fct"), "x")


def _prop(
    name: str, depends_on: tuple[PropertyRef[Any, Any], ...] = ()
) -> Property[bool, ColumnRef]:
    return column_property(
        name=name,
        lattice=_BOOL,
        operators={},
        aggregates={},
        ground=lambda _c: Annotation(False),
        depends_on=depends_on,
    )


# --- AnnotationStore ---------------------------------------------------------


def test_store_record_then_get() -> None:
    store = AnnotationStore()
    ann = Annotation(True)
    store.record("nullability", _COL, ann)
    assert store.get("nullability", _COL) == ann


def test_store_get_missing_is_none() -> None:
    store = AnnotationStore()
    assert store.get("nullability", _COL) is None


# --- evaluation order --------------------------------------------------------


def test_evaluation_order_puts_dependencies_first() -> None:
    base = _prop("base")
    derived = _prop("derived", depends_on=(base.ref,))
    order = PropertyRegistry((derived, base)).evaluation_order()
    names = [p.name for p in order]
    assert names.index("base") < names.index("derived")


def test_evaluation_order_independent_properties_keep_input_order() -> None:
    a, b = _prop("a"), _prop("b")
    order = PropertyRegistry((a, b)).evaluation_order()
    assert [p.name for p in order] == ["a", "b"]


def test_duplicate_name_fails_assembly() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        PropertyRegistry((_prop("dup"), _prop("dup"))).evaluation_order()


def test_dangling_edge_fails_assembly() -> None:
    """An edge to a property that is not registered at all is a build error."""
    stranger = _prop("stranger")
    consumer = _prop("consumer", depends_on=(stranger.ref,))
    with pytest.raises(ValueError, match=r"not a registered"):
        PropertyRegistry((consumer,)).evaluation_order()


def test_forged_ref_with_matching_name_fails_assembly() -> None:
    """The edge is checked by ref identity, not by name: a different ref object
    that merely shares a registered property's name still fails."""
    real = _prop("dep")
    impostor = _prop("dep")  # equal by name, distinct object
    consumer = _prop("consumer", depends_on=(impostor.ref,))
    assert impostor.ref == real.ref  # equal by name
    assert impostor.ref is not real.ref  # but a distinct object
    with pytest.raises(ValueError, match=r"not a registered"):
        PropertyRegistry((consumer, real)).evaluation_order()


def test_cycle_fails_assembly() -> None:
    base = _prop("base")
    consumer = _prop("consumer", depends_on=(base.ref,))
    # Close the loop by pointing base back at consumer; replace keeps base.ref's
    # identity so consumer's edge still resolves to this object.
    looped_base = dataclasses.replace(base, depends_on=(consumer.ref,))
    with pytest.raises(ValueError, match="cycle"):
        PropertyRegistry((consumer, looped_base)).evaluation_order()


# --- dep_context -------------------------------------------------------------


def test_dep_context_reads_recorded_annotation() -> None:
    base = _prop("base")
    registry = PropertyRegistry((base,))
    store = AnnotationStore()
    store.record("base", _COL, Annotation(True))
    ctx = registry.dep_context(store)
    assert ctx.annotation(base.ref, _COL) == Annotation(True)


def test_dep_context_silent_dependency_is_none() -> None:
    base = _prop("base")
    registry = PropertyRegistry((base,))
    ctx = registry.dep_context(AnnotationStore())
    assert ctx.annotation(base.ref, _COL) is None
