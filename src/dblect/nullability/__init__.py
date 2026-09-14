"""Fact-grounded audit detectors for nullability hazards.

The nullability property marks each column NON_NULL, NULLABLE, or unknown: NULLABLE
covers a column an outer join can pad with NULLs, and NON_NULL can hold only once a
condition the query establishes guards it. It is computed in
``dblect.lineage.properties.nullability``. This package is the audit-facing consumer:
detectors that read the proven nullability and flag NULL-sensitive constructs where the
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
