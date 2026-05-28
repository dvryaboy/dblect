"""Where-provenance: for every output column, the set of source columns
whose values feed it.

The vocabulary is from Cheney, Chiticariu, and Tan 2009 (where-provenance
is "which source cells contributed to this output cell"), narrowed here to
the column level, which is what we care about for SQL hazard detection.

The implementation is the simplest interesting instance of the substrate:
the value type is ``frozenset[ColumnRef]``, the semiring is set-union
(both ``plus`` and ``times`` are union), a leaf column annotates itself as
``{self}``, and no operator or aggregate has special handling. Every
expression and JOIN just unions its inputs' source-column sets, so the
final annotation on an output column is exactly the closure of source
columns that fed it.

Two things this earns us at this stage:

* End-to-end cross-validation of the substrate. The propagator computes
  each output column's source set by walking the projection expression;
  the builder records the same set independently by walking sqlglot's
  lineage chain to leaves. They have to agree, and that's the main
  soundness check before properties with interesting per-operator behaviour
  (nullability, uniqueness, cardinality) land.
* A cheap "did this column come from X?" primitive for downstream
  detectors and reporters, without re-walking SQL.
"""

from __future__ import annotations

from dblect.lineage.graph import ColumnRef
from dblect.lineage.property import Property
from dblect.lineage.semiring import UnionSemiring

WhereProvenance = frozenset[ColumnRef]


def _source(col: ColumnRef) -> WhereProvenance:
    return frozenset({col})


where_provenance: Property[WhereProvenance] = Property(
    name="where_provenance",
    semiring=UnionSemiring[ColumnRef](),
    source=_source,
    operators={},  # default fold via semiring.times == set union
    aggregates={},  # aggregates likewise union their inputs
    unknown_value=frozenset(),
)
"""The where-provenance property.

Operators and aggregates both fall through to the walker's default
child-fold, which under ``UnionSemiring`` is set union. A leaf column
annotates itself with ``{self}``; downstream columns inherit the union of
their inputs.
"""
