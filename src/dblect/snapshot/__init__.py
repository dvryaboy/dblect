"""Manifest-grounded audit detector for dbt snapshots.

A snapshot carries SCD-2 validity columns, so a downstream query that reads it without
restricting to the current row or a point-in-time slice silently fans out one row per
historical version. This package is the audit-facing consumer: it reads each snapshot's
validity columns from the manifest (honoring ``snapshot_meta_column_names`` renames) and
flags reads that omit a temporal filter.
"""

from dblect.snapshot.detector import (
    detect_snapshot_temporal_filter,
    make_snapshot_detectors,
)

__all__ = [
    "detect_snapshot_temporal_filter",
    "make_snapshot_detectors",
]
