"""Check a project both ways an incremental dbt model can compile, and diff the
findings.

A ``{% if is_incremental() %}`` block means a model's SQL can be genuinely
different text on a full refresh versus a steady-state incremental run, not just
the same SQL with a different value substituted in (that case is what
:mod:`dblect.check.worlds` handles, and each of its worlds is built independently
from its own manifest). This module compiles the project both ways and runs every
detector over each compile through the one shared entry point,
:func:`dblect.analysis.analyze`, so both detector families are present by
construction rather than by this module remembering to call each. Because the SQL
text differs between the two compiles, a finding's message and line span can drift
even when it is the same underlying issue, so the diff matches on the stable
:data:`~dblect.analysis.FindingIdentity` rather than comparing findings for exact
equality. Issue #107 weighs unifying the finding representations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dblect.adapters import AdapterProfile
from dblect.analysis import AnalysisFinding, FindingIdentity, analyze, cross_world_identity
from dblect.execution.incremental import (
    CompiledWorld,
    IncrementalWorldCompilation,
    compile_incremental_worlds,
)
from dblect.lineage.facts.model import WorldRef
from dblect.types import ContractRegistry


@dataclass(frozen=True, slots=True)
class CrossWorldFinding:
    """A finding that holds in some of the checked worlds but not all of them (for
    example, only on a full refresh, not on steady state).

    ``worlds`` are the worlds the finding holds under, and ``representative`` is one
    world's instance of it for display (the message and span are that world's). A
    finding present in every checked world is not a ``CrossWorldFinding``; the
    ordinary single-manifest analysis already reports it.
    """

    identity: FindingIdentity
    representative: AnalysisFinding
    worlds: frozenset[WorldRef]


def cross_world_findings(
    per_world: Mapping[WorldRef, Sequence[AnalysisFinding]],
) -> tuple[CrossWorldFinding, ...]:
    """The findings that hold in some of ``per_world``'s worlds but not all of them.

    Findings are grouped by :func:`~dblect.analysis.cross_world_identity` so a message
    or line span that drifts between the two compiled SQLs is not mistaken for a
    distinct finding. The result is ordered deterministically by identity.
    """
    analyzed = frozenset(per_world)
    worlds_by_identity: dict[FindingIdentity, set[WorldRef]] = {}
    representative: dict[FindingIdentity, AnalysisFinding] = {}
    for world, findings in per_world.items():
        for finding in findings:
            identity = cross_world_identity(finding)
            worlds_by_identity.setdefault(identity, set()).add(world)
            representative.setdefault(identity, finding)
    varying = [
        CrossWorldFinding(
            identity=identity,
            representative=representative[identity],
            worlds=frozenset(worlds),
        )
        for identity, worlds in worlds_by_identity.items()
        if frozenset(worlds) != analyzed
    ]
    return tuple(sorted(varying, key=lambda finding: str(finding.identity)))


@dataclass(frozen=True, slots=True)
class IncrementalWorldCheck:
    """The result of checking a project across its incremental worlds: the per-world
    findings and the compilation that produced them (for opaque-world diagnostics)."""

    per_world: Mapping[WorldRef, tuple[AnalysisFinding, ...]]
    compilation: IncrementalWorldCompilation

    @property
    def analyzed_worlds(self) -> frozenset[WorldRef]:
        """The worlds that compiled and were checked."""
        return frozenset(self.per_world)

    @property
    def opaque_worlds(self) -> tuple[CompiledWorld, ...]:
        """The worlds whose compile did not succeed, carrying their dbt error. A
        cross-world comparison needs both worlds, so an opaque world is reported
        rather than allowed to masquerade as agreement."""
        worlds = (self.compilation.full_refresh, self.compilation.steady_state)
        return tuple(world for world in worlds if not world.ok)

    def cross_world_findings(self) -> tuple[CrossWorldFinding, ...]:
        """The findings that hold in some analyzed worlds but not all: the "holds in
        one world, breaks in the other" signal."""
        return cross_world_findings(self.per_world)


def check_incremental_worlds(
    project_dir: Path,
    profile: AdapterProfile,
    *,
    registry: ContractRegistry | None = None,
    dbt_executable: str = "dbt",
) -> IncrementalWorldCheck:
    """Compile ``project_dir`` both ways an incremental model can build (full
    refresh and steady state) and check each.

    ``profile`` is the resolved target whose dialect parses every model, and
    ``registry`` the contracts to resolve (defaulting to the active one), the same
    inputs :func:`dblect.analysis.analyze` takes. Compilation is data-free and needs
    no warehouse connection (see :mod:`dblect.execution.incremental`); a world whose
    compile failed is omitted from the per-world findings and surfaced through
    :attr:`IncrementalWorldCheck.opaque_worlds`.
    """
    compilation = compile_incremental_worlds(project_dir, dbt_executable=dbt_executable)
    per_world: dict[WorldRef, tuple[AnalysisFinding, ...]] = {}
    for world, manifest in compilation.manifests().items():
        per_world[world] = analyze(manifest, profile, registry=registry).findings
    return IncrementalWorldCheck(per_world=per_world, compilation=compilation)
