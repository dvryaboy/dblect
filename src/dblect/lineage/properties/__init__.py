"""Concrete properties the lineage propagator can compute.

Each property pairs an algebraic interface (a ``Semiring``) with rules for
how values combine at operators and aggregates. The propagator in
``dblect.lineage.property`` consumes them generically: one walker, many
properties.

The current set:

* ``where_provenance``: production. Set-union over leaf ``ColumnRef``s.
* ``nullability``: demo. Tri-state {NON_NULL, NULLABLE, UNKNOWN}; covers
  ``COALESCE``, ``IS NOT NULL``, ``COUNT``, and the default times/plus
  folds. Used to demonstrate that the substrate's structural reshape
  (#25) unlocks property propagation through CTE and UNION layers; not
  intended for audit consumption until the gaps in its docstring are
  filled.
* ``aggregation_depth``: demo. Max-semiring over int depth; +1 per
  ``AggFunc``. Used to demonstrate that nested aggregates through a CTE
  surface as depth > 1; not intended for audit consumption until the gaps
  in its docstring are filled.
"""

from dblect.lineage.properties.aggregation_depth import aggregation_depth
from dblect.lineage.properties.nullability import Nullability, nullability
from dblect.lineage.properties.where_provenance import where_provenance

__all__ = [
    "Nullability",
    "aggregation_depth",
    "nullability",
    "where_provenance",
]
