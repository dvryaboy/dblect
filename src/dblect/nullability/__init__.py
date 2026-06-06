"""Fact-grounded audit detectors for nullability hazards.

The nullability *property* (per-column tri-state, with outer-join taint and conditional
NON_NULL activation) lives on the lineage.facts substrate as
``dblect.lineage.properties.nullability``. This package is the audit-facing consumer:
detectors that read the proven nullability and flag NULL-sensitive constructs where the
null silently changes the result: a GROUP BY on an inherited-nullable key, a join keyed
on one, and a ``NOT IN`` over a subquery that projects one.
"""

from dblect.nullability.detector import (
    detect_join_on_nullable_key,
    detect_not_in_nullable_subquery,
    detect_null_group_on_nullable_key,
    make_nullability_detectors,
)

__all__ = [
    "detect_join_on_nullable_key",
    "detect_not_in_nullable_subquery",
    "detect_null_group_on_nullable_key",
    "make_nullability_detectors",
]
