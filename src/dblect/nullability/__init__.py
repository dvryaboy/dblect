"""Fact-grounded audit detectors for nullability hazards.

The nullability *property* tracks, per column, whether it can be NULL: an outer join
can make it NULL, and a NOT NULL declared only under a filter activates once a
downstream query applies that filter. Implemented in
``dblect.lineage.properties.nullability``. This package is the audit-facing consumer:
detectors that read the proven nullability and flag NULL-sensitive constructs where a
null silently changes the result: a GROUP BY on an inherited-nullable key, a join keyed
on one, a ``NOT IN`` over a subquery that projects one, and a ``NOT EXISTS`` whose probe
correlation key is one.
"""

from dblect.nullability.detector import (
    detect_join_on_nullable_key,
    detect_not_exists_on_nullable_key,
    detect_not_in_nullable_subquery,
    detect_null_group_on_nullable_key,
    make_nullability_detectors,
)

__all__ = [
    "detect_join_on_nullable_key",
    "detect_not_exists_on_nullable_key",
    "detect_not_in_nullable_subquery",
    "detect_null_group_on_nullable_key",
    "make_nullability_detectors",
]
