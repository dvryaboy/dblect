"""Check a project across its incremental worlds.

The incremental axis compiles a project two ways (full-refresh and steady-state)
via :func:`dblect.execution.incremental.compile_incremental_worlds`, runs the
single-world checker over each, and differences the findings: a finding that
holds in one world and not the other is the cross-world signal this stream
surfaces, while a finding present in both worlds is the one the single-manifest
analyzer already reports.

These are control-flow worlds (the SQL itself differs), so each world is built
and checked independently from its own manifest, rather than the shared build the
value-substitution enumerator (:func:`dblect.check.worlds.enumerate_worlds`) uses.
The differencing is the same :meth:`~dblect.check.worlds.EnumeratedFindings.world_varying`
view, here over per-world builds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dblect.adapters import AdapterProfile
from dblect.check.findings import CheckFinding
from dblect.check.run import run_check
from dblect.check.worlds import EnumeratedFindings, WorldResult
from dblect.execution.incremental import (
    CompiledWorld,
    IncrementalWorldCompilation,
    compile_incremental_worlds,
)
from dblect.lineage.facts.model import WorldRef
from dblect.types import ContractRegistry


@dataclass(frozen=True, slots=True)
class IncrementalWorldCheck:
    """The result of checking a project across its incremental worlds: the per-world
    findings and the compilation that produced them (for opaque-world diagnostics)."""

    enumerated: EnumeratedFindings
    compilation: IncrementalWorldCompilation

    @property
    def analyzed_worlds(self) -> frozenset[WorldRef]:
        """The worlds that compiled and were checked."""
        return frozenset(result.world for result in self.enumerated.per_world)

    @property
    def opaque_worlds(self) -> tuple[CompiledWorld, ...]:
        """The worlds whose compile did not succeed, carrying their dbt error. A
        cross-world comparison needs both worlds, so an opaque world is reported
        rather than allowed to masquerade as agreement."""
        worlds = (self.compilation.full_refresh, self.compilation.steady_state)
        return tuple(world for world in worlds if not world.ok)

    def cross_world_findings(self) -> Mapping[CheckFinding, frozenset[WorldRef]]:
        """The findings that hold in some analyzed worlds but not all, each mapped to
        the worlds it holds under: the "holds in one world, fails in the other" signal."""
        return self.enumerated.world_varying()


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
    inputs :func:`~dblect.check.run.run_check` takes. Compilation is data-free and
    needs no warehouse connection (see :mod:`dblect.execution.incremental`); a world
    whose compile failed is omitted from the per-world findings and surfaced through
    :attr:`IncrementalWorldCheck.opaque_worlds`.
    """
    compilation = compile_incremental_worlds(project_dir, dbt_executable=dbt_executable)
    results = tuple(
        WorldResult(
            world=world,
            findings=tuple(run_check(manifest, profile, registry=registry).findings),
        )
        for world, manifest in compilation.manifests().items()
    )
    return IncrementalWorldCheck(
        enumerated=EnumeratedFindings(per_world=results), compilation=compilation
    )
