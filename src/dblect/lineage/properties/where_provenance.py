"""Where-provenance: for every column, the set of source columns whose
values feed it.

The vocabulary is from Cheney, Chiticariu, and Tan 2009, narrowed to the
column level. Value type is ``frozenset[ColumnRef]``; the semiring is
set-union (both ``plus`` and ``times`` are union). No operator or
aggregate is special-cased: every expression and JOIN unions its inputs'
source-column sets, so the annotation on an output column is the closure
of source columns that fed it.
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
