"""Concrete properties the lineage propagator can compute.

Each property pairs an algebraic interface (a ``Semiring``) with rules for
how values combine at operators and aggregates. The propagator in
``dblect.lineage.property`` consumes them generically: one walker, many
properties.

``where_provenance`` is production. ``nullability_property`` reads a manifest for
its grounding; ``aggregation_depth`` is a demo property whose module docstring
lists the operator-coverage gaps that keep it out of audit consumption.
"""

from dblect.lineage.properties.aggregation_depth import aggregation_depth
from dblect.lineage.properties.nullability import (
    Nullability,
    native_not_null_discoverer,
    not_null_test_discoverer,
    nullability_property,
)
from dblect.lineage.properties.where_provenance import where_provenance

__all__ = [
    "Nullability",
    "aggregation_depth",
    "native_not_null_discoverer",
    "not_null_test_discoverer",
    "nullability_property",
    "where_provenance",
]
