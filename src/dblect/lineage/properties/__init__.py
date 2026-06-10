"""Concrete properties the lineage propagator can compute.

Each property pairs an algebraic interface (a ``Semiring``) with rules for
how values combine at operators and aggregates. The propagator in
``dblect.lineage.property`` consumes them generically: one walker, many
properties.

``where_provenance`` is production. ``nullability_property`` and
``uniqueness_property`` read a manifest for their grounding; ``aggregation_depth``
is a demo property whose module docstring lists the operator-coverage gaps that
keep it out of audit consumption. ``uniqueness_property`` is relation-scoped and
carries its relation-algebra walk on the property itself, so the propagator
dispatches it with no global registration step.
"""

from dblect.lineage.properties.aggregation_depth import aggregation_depth
from dblect.lineage.properties.domain_type import (
    CONFLICT,
    NAKED,
    Concrete,
    Dimension,
    DomainTag,
    PerRow,
    Tagged,
    domain_type_grounding,
    domain_type_property,
    tagged,
)
from dblect.lineage.properties.nullability import (
    Nullability,
    native_not_null_discoverer,
    not_null_test_discoverer,
    nullability_property,
)
from dblect.lineage.properties.uniqueness import (
    CandidateKeySet,
    native_key_discoverer,
    unique_combination_discoverer,
    unique_test_discoverer,
    uniqueness_property,
)
from dblect.lineage.properties.where_provenance import where_provenance

__all__ = [
    "CONFLICT",
    "NAKED",
    "CandidateKeySet",
    "Concrete",
    "Dimension",
    "DomainTag",
    "Nullability",
    "PerRow",
    "Tagged",
    "aggregation_depth",
    "domain_type_grounding",
    "domain_type_property",
    "native_key_discoverer",
    "native_not_null_discoverer",
    "not_null_test_discoverer",
    "nullability_property",
    "tagged",
    "unique_combination_discoverer",
    "unique_test_discoverer",
    "uniqueness_property",
    "where_provenance",
]
