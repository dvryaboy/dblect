"""Fact-grounded audit detectors for uniqueness hazards.

The uniqueness *facts* (candidate keys per relation, declared and propagated)
live on the lineage.facts substrate as ``dblect.lineage.properties.uniqueness``.
This package is the audit-facing consumer: detectors that read those keys and
flag window order-key, join-fanout, and non-deterministic ``LIMIT`` hazards on a
project's SQL.
"""

from dblect.uniqueness.detector import (
    detect_join_fanout,
    detect_limit_without_deterministic_order,
    detect_non_unique_aggregate_order_keys,
    detect_non_unique_window_order_keys,
    make_fact_grounded_detectors,
)

__all__ = [
    "detect_join_fanout",
    "detect_limit_without_deterministic_order",
    "detect_non_unique_aggregate_order_keys",
    "detect_non_unique_window_order_keys",
    "make_fact_grounded_detectors",
]
