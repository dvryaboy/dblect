"""The ``dblect check`` pipeline: resolve contracts, propagate, report findings.

Loads nothing itself; a caller (the CLI) populates a registry via the loader and
passes it in, or relies on the active one. See ``run_check`` for the orchestration
and ``docs/design/declaration-dsl.md`` for the findings it surfaces.
"""

from __future__ import annotations

from dblect.check.findings import CheckFinding, CheckFindingKind, CheckReport
from dblect.check.report import JSON_SCHEMA_VERSION, render_json, render_text
from dblect.check.run import run_check

__all__ = [
    "JSON_SCHEMA_VERSION",
    "CheckFinding",
    "CheckFindingKind",
    "CheckReport",
    "render_json",
    "render_text",
    "run_check",
]
