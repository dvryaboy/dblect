"""Check a project across its incremental worlds: compile both ways, run the
project's detectors over each, and difference the findings.

These are control-flow worlds, so each is built independently from its own manifest
(the shared-build enumerator in :mod:`dblect.check.worlds` serves value-substitution
worlds, where the SQL is identical). Each world's findings come through the single
analysis door, :func:`dblect.analysis.analyze`, so both detector families are present
by construction rather than by this module remembering to call each. Because the SQL
differs, a finding's message and line span drift between worlds even when the issue is
the same, so the diff keys on a stable :data:`~dblect.analysis.FindingIdentity` rather
than whole-finding equality. Issue #107 weighs unifying the finding representations.
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
    """A finding that holds in a strict subset of the analyzed worlds.

    ``worlds`` are the worlds the finding holds under, and ``representative`` is one
    world's instance of it for display (the message and span are that world's). A
    finding present in every analyzed world is world-invariant and is not a
    ``CrossWorldFinding``; the single-manifest analysis already reports it.
    """

    identity: FindingIdentity
    representative: AnalysisFinding
    worlds: frozenset[WorldRef]


def cross_world_findings(
    per_world: Mapping[WorldRef, Sequence[AnalysisFinding]],
) -> tuple[CrossWorldFinding, ...]:
    """The findings holding in a strict subset of ``per_world``'s worlds.

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
    """Compile ``project_dir`` into its incremental worlds and check each.

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
