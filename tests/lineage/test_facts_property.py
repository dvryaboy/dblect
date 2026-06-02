"""Property construction: ref minting, scope fixing, and the semiring invariant.

These pin the contracts the registry and propagator rest on: a ``PropertyRef``
is un-forgeable, the smart constructors fix ``scope_kind`` to match the scope
type, refs compare by name (so the registry can reject duplicates), and a
semiring-carrying property may not also pin the relational operators by hand.
"""

from __future__ import annotations

import pytest
import sqlglot.expressions as exp

from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, ScopeKind
from dblect.lineage.facts.property import (
    PropertyRef,
    column_property,
    relation_property,
)
from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.semiring import UnionSemiring

_BOOL_LATTICE: Lattice[bool] = Lattice(
    meet=lambda a, b: a and b,
    join=lambda a, b: a or b,
    top=False,
    bottom=True,
)


def _ground_col(_: ColumnRef) -> Annotation[bool]:
    return Annotation(False)


def _ground_rel(_: SourceRef) -> Annotation[bool]:
    return Annotation(False)


def test_property_ref_cannot_be_built_directly() -> None:
    """Without the module-private mint token, PropertyRef construction is a TypeError."""
    with pytest.raises(TypeError):
        PropertyRef(name="forged", _mint=object())


def test_column_property_mints_ref_and_fixes_scope() -> None:
    prop = column_property(
        name="nullability",
        lattice=_BOOL_LATTICE,
        operators={},
        aggregates={},
        ground=_ground_col,
    )
    assert prop.scope_kind is ScopeKind.COLUMN
    assert prop.name == "nullability"
    assert prop.ref.name == "nullability"


def test_relation_property_fixes_relation_scope() -> None:
    prop = relation_property(
        name="uniqueness",
        lattice=_BOOL_LATTICE,
        operators={},
        aggregates={},
        ground=_ground_rel,
    )
    assert prop.scope_kind is ScopeKind.RELATION
    assert prop.name == "uniqueness"


def test_refs_compare_by_name() -> None:
    """Two properties minted with the same name carry equal refs, which is how
    the registry detects a duplicate name."""
    a = column_property(
        name="dup", lattice=_BOOL_LATTICE, operators={}, aggregates={}, ground=_ground_col
    )
    b = column_property(
        name="dup", lattice=_BOOL_LATTICE, operators={}, aggregates={}, ground=_ground_col
    )
    assert a.ref == b.ref
    # ...but they are distinct objects, so identity still separates them.
    assert a.ref is not b.ref


def test_semiring_property_rejects_redefined_relational_operators() -> None:
    """A semiring-carrying property derives confluence/cross from the semiring, so
    pinning exp.Union or exp.Join in operators is a construction error."""
    with pytest.raises(ValueError, match="semiring"):
        column_property(
            name="where_provenance",
            lattice=_BOOL_LATTICE,
            operators={exp.Union: lambda _e, ks, _d: ks[0]},
            aggregates={},
            ground=_ground_col,
            semiring=UnionSemiring[ColumnRef](),  # type: ignore[arg-type]
        )
