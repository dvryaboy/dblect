"""The ``dblect check`` pipeline: resolve contracts, propagate, report findings.

Loads nothing itself; a caller (the CLI) populates a registry via the loader and
passes it in, or relies on the active one. See ``run_check`` for the orchestration
and ``docs/design/declaration-dsl.md`` for the findings it surfaces.
"""

from __future__ import annotations

from dblect.check.findings import CheckFinding, CheckFindingKind, CheckReport, UnbuiltModel
from dblect.check.flags import DomainFlag, check_worlds, flag_worlds, lower_flag
from dblect.check.run import (
    CheckGraphs,
    WorldAnnotations,
    WorldFacts,
    base_world_facts,
    build_check_graphs,
    propagate_world,
    run_check,
    world_findings,
)
from dblect.check.worlds import (
    CompileFact,
    EnumeratedFindings,
    FdCompileFact,
    TagCompileFact,
    WorldResult,
    enumerate_worlds,
)

__all__ = [
    "CheckFinding",
    "CheckFindingKind",
    "CheckGraphs",
    "CheckReport",
    "CompileFact",
    "DomainFlag",
    "EnumeratedFindings",
    "FdCompileFact",
    "TagCompileFact",
    "UnbuiltModel",
    "WorldAnnotations",
    "WorldFacts",
    "WorldResult",
    "base_world_facts",
    "build_check_graphs",
    "check_worlds",
    "enumerate_worlds",
    "flag_worlds",
    "lower_flag",
    "propagate_world",
    "run_check",
    "world_findings",
]
