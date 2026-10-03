"""Where-provenance: for every column, the set of source columns that feed it.

The vocabulary is from Cheney, Chiticariu, and Tan 2009, narrowed to the column
level. The value type is ``frozenset[ColumnRef]`` and the engine is the union
semiring (both ``plus`` and ``times`` are union), so every operator and join
unions its inputs' source-column sets and the annotation on an output column is
the closure of source columns that fed it.

The one intrinsic fact where-provenance carries is the leaf seed: a base-relation
column (a source, seed, or snapshot) traces to itself, so it grounds to the
singleton ``{col}``. Derived columns declare nothing and ground IMPLICIT-empty, so
the walk's union closure stands. The lattice is otherwise nominal: the only field
the propagator reads at a derived node is ``top`` (the empty set, "no
provenance"), used to classify an empty result as carrying no information. The
combine laws are the union-semiring laws, checked in ``test_semiring_laws``.
"""

from __future__ import annotations

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Opacity
from dblect.lineage.facts.property import Property, column_property
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceKind
from dblect.lineage.property import resolved_column_ref
from dblect.lineage.semiring import UnionSemiring

WhereProvenance = frozenset[ColumnRef]

_EMPTY: WhereProvenance = frozenset()
# Base relations: a column on one of these traces to itself.
_BASE_KINDS = frozenset({SourceKind.SOURCE, SourceKind.SEED, SourceKind.SNAPSHOT})

# Nominal lattice: where-provenance is driven entirely by its union semiring. Only
# ``top`` (the empty set) is read at runtime; meet/join/bottom are inert because
# the only facts are the leaf self-seeds, which never need resolving against each
# other.
_PROVENANCE_LATTICE: Lattice[WhereProvenance] = Lattice(
    meet=lambda a, b: a | b,
    join=lambda a, b: a & b,
    top=_EMPTY,
    bottom=_EMPTY,
)


def _ground(col: ColumnRef) -> Annotation[WhereProvenance]:
    if col.source.kind in _BASE_KINDS:
        return Annotation(frozenset({col}), Opacity.CONCRETE)
    return Annotation(_EMPTY, Opacity.IMPLICIT)


where_provenance: Property[WhereProvenance, ColumnRef] = column_property(
    name="where_provenance",
    lattice=_PROVENANCE_LATTICE,
    operators={},  # every operator folds via semiring.times == set union
    aggregates={},  # aggregates likewise union their inputs
    ground=_ground,
    semiring=UnionSemiring[ColumnRef](),
)


# Wrappers that keep a column's value a function of that one column alone.
_VALUE_WRAPPERS = (exp.Alias, exp.Paren, exp.Cast, exp.TryCast)


def value_origin(graph: ColumnLineageGraph, col: ColumnRef) -> ColumnRef | None:
    """The base column whose value ``col`` is a deterministic function of, alone, or ``None``.

    Follows a chain of bare references, aliases, parentheses and casts down to a base
    relation's column. Where-provenance cannot say this: it also lists every column an
    expression merely reads (``CONCAT(x, y)``, a window ordered by ``x``). A relation
    unique on a function of ``x`` is unique on ``x`` too, which is what lets a key be
    re-expressed through it. Any other derivation (a call, a window, a union) returns
    ``None``, the safe direction.
    """
    seen: set[ColumnRef] = set()
    while col not in seen:
        seen.add(col)
        node = graph.derivation(col)
        if node is None:
            return col if col.source.kind in _BASE_KINDS else None
        while isinstance(node, _VALUE_WRAPPERS) and isinstance(node.this, Expr):
            node = node.this
        if not isinstance(node, exp.Column):
            return None
        nxt = resolved_column_ref(node)
        if nxt is None:
            return None
        col = nxt
    return None
