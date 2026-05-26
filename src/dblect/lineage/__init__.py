"""Column-level lineage for dbt projects, plus a generic engine for
propagating *properties* (where-provenance, nullability, uniqueness,
fanout, ...) over that lineage.

Two pieces:

* A ``ColumnLineageGraph`` built from each model's compiled SQL. Per output
  column it records which upstream columns fed it (edges) and the sqlglot
  expression that built it (so a property can walk it).
* A ``Property[K]`` API plus a single ``propagate`` walker. A property says
  what value a leaf column starts with and how values combine across
  operators and aggregates. Adding a new propagated property is adding a
  ``Property[K]`` value; the walker does not change.

The combine-rules satisfy the laws of a commutative semiring; aggregates use
a semimodule on top. The framework is from Green, Karvounarakis, and Tannen
("Provenance Semirings", PODS 2007); the aggregate extension is from
Amsterdamer, Deutch, and Tannen ("Provenance for Aggregate Queries", PODS
2011). You don't need either paper to write a property; see
``docs/design/column-level-lineage.md`` for the practical guide.
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
