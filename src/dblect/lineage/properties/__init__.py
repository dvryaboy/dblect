"""Concrete properties the lineage propagator can compute.

Each property pairs an algebraic interface (a ``Semiring``) with rules for
how values combine at operators and aggregates. The propagator in
``dblect.lineage.property`` consumes them generically: one walker, many
properties.
"""

from dblect.lineage.properties.where_provenance import where_provenance

__all__ = ["where_provenance"]
