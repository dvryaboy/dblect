"""Where-provenance: which source ``ColumnRef``s ultimately fed each output column.

Encoded as the set-union semiring over ``frozenset[ColumnRef]`` (the most
elementary non-trivial K-relations instance). Per Cheney, Chiticariu, and
Tan 2009's vocabulary, this is *where-provenance* in the column-level sense:
the set of source-level columns whose values fed into a given output column.

For the V0 substrate this property does two things:

* Cross-validates the propagator end-to-end against sqlglot's own lineage
  walker. The propagator's annotation for an output column should equal the
  union of the builder-recorded edge leaves for that column.
* Provides a cheap "what did this come from" primitive that downstream
  detectors and reporters can use without re-walking SQL.

The interesting per-operator behaviour appears once we add nullability,
uniqueness, or cardinality; where-provenance unions across every operator.
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

Every operator and aggregate falls through to the default child-fold, which
under ``UnionSemiring`` is just set union. Source columns annotate themselves
as a singleton set containing their own ``ColumnRef``; downstream columns
inherit the union of upstream sets.
"""
