"""Column-level lineage and property propagation.

This package builds on the provenance-semiring framework (Green, Karvounarakis,
Tannen 2007 "Provenance Semirings", PODS) and the semimodule extension for
aggregates (Amsterdamer, Deutch, Tannen 2011 "Provenance for Aggregate
Queries", PODS), applied to dbt model graphs via sqlglot's parse + ``lineage``
primitives.

The substrate is two pieces:

* A ``ColumnLineageGraph`` (where- and how-provenance per output column) built
  from each model's compiled SQL.
* A ``Property[K]`` API plus a single ``propagate`` walker. A property is a
  commutative semiring ``(K, +, x, 0, 1)`` paired with per-operator and
  per-aggregate transfer functions, dispatched on sqlglot expression types.
  Adding a new propagated property (uniqueness, nullability, fanout, semantic
  tag) is adding a ``Property[K]`` value; the walker does not change.

See ``docs/design/column-level-lineage.md`` for the design.
"""

from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceKind, SourceRef
from dblect.lineage.property import Property, propagate
from dblect.lineage.semiring import BooleanSemiring, Semiring, UnionSemiring

__all__ = [
    "BooleanSemiring",
    "ColumnLineageGraph",
    "ColumnRef",
    "Property",
    "Semiring",
    "SourceKind",
    "SourceRef",
    "UnionSemiring",
    "propagate",
]
