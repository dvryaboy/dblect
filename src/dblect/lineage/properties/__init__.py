"""Instantiated ``Property[K]`` values.

Each property pairs a semiring with the per-operator and per-aggregate
transfer functions specific to that property. The propagator in
``dblect.lineage.property`` consumes them generically.
"""

from dblect.lineage.properties.where_provenance import where_provenance

__all__ = ["where_provenance"]
