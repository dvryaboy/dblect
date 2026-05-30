"""Concrete properties the lineage propagator can compute.

Each property pairs an algebraic interface (a ``Semiring``) with rules for
how values combine at operators and aggregates. The propagator in
``dblect.lineage.property`` consumes them generically: one walker, many
properties.

``where_provenance`` is production. ``nullability`` and
``aggregation_depth`` are demo properties — their module docstrings list
the operator-coverage gaps that keep them out of audit consumption.
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
