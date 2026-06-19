"""Check a project across its incremental worlds: compile both ways, run both
detector families (:func:`~dblect.check.run.run_check` and
:func:`~dblect.audit.run_audit`) over each, and difference the findings.

These are control-flow worlds, so each is built independently from its own manifest
(the shared-build enumerator in :mod:`dblect.check.worlds` serves value-substitution
worlds, where the SQL is identical). Because the SQL differs, a finding's message
and line span drift between worlds even when the issue is the same, so the diff keys
on a stable :data:`FindingIdentity` rather than whole-finding equality. Issue #107
tracks unifying the two finding representations this identity spans.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dblect.adapters import AdapterProfile
from dblect.audit import LocatedFinding, run_audit
from dblect.check.findings import CheckFinding
from dblect.check.run import run_check
from dblect.execution.incremental import (
    CompiledWorld,
    IncrementalWorldCompilation,
    compile_incremental_worlds,
)
from dblect.lineage.facts.model import WorldRef
from dblect.types import ContractRegistry

# A finding observed in one world: either a declaration-level finding from
# ``run_check`` or a located SQL-structural finding from ``run_audit``.
IncrementalFinding = CheckFinding | LocatedFinding

# A world-independent identity for a finding, used to recognize "the same finding
# in two worlds" while ignoring the parts that drift between two compilations: the
# free-text message and (for structural findings) the line span. Two findings with
# equal identity are treated as the same issue across worlds.
FindingIdentity = tuple[Hashable, ...]


def finding_identity(finding: IncrementalFinding) -> FindingIdentity:
    """The stable cross-world identity of ``finding``: where it lands and what it is,
    without the message or line span that differ between two compilations. A
    declaration-level finding keys on kind/model/column/contract; a structural one on
    kind/model and the rendered offending snippet. A snippet present in one world only
    (a steady-state-only join) has no match in the other, so it surfaces as varying.
    """
    if isinstance(finding, CheckFinding):
        return ("check", finding.kind, finding.model_unique_id, finding.column, finding.contract)
    inner = finding.finding
    return ("audit", inner.kind, finding.model_unique_id, inner.sql_snippet)


@dataclass(frozen=True, slots=True)
class CrossWorldFinding:
    """A finding that holds in a strict subset of the analyzed worlds.

    ``worlds`` are the worlds the finding holds under, and ``representative`` is one
    world's instance of it for display (the message and span are that world's). A
    finding present in every analyzed world is world-invariant and is not a
    ``CrossWorldFinding``; the single-manifest analysis already reports it.
    """

    identity: FindingIdentity
    representative: IncrementalFinding
    worlds: frozenset[WorldRef]


def cross_world_findings(
    per_world: Mapping[WorldRef, Sequence[IncrementalFinding]],
) -> tuple[CrossWorldFinding, ...]:
    """The findings holding in a strict subset of ``per_world``'s worlds.

    Findings are grouped by :func:`finding_identity` so a message or line span that
    drifts between the two compiled SQLs is not mistaken for a distinct finding. The
    result is ordered deterministically by identity.
    """
    analyzed = frozenset(per_world)
    worlds_by_identity: dict[FindingIdentity, set[WorldRef]] = {}
    representative: dict[FindingIdentity, IncrementalFinding] = {}
    for world, findings in per_world.items():
        for finding in findings:
            identity = finding_identity(finding)
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

    per_world: Mapping[WorldRef, tuple[IncrementalFinding, ...]]
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
    inputs :func:`~dblect.check.run.run_check` takes. Compilation is data-free and
    needs no warehouse connection (see :mod:`dblect.execution.incremental`); a world
    whose compile failed is omitted from the per-world findings and surfaced through
    :attr:`IncrementalWorldCheck.opaque_worlds`.
    """
    compilation = compile_incremental_worlds(project_dir, dbt_executable=dbt_executable)
    per_world: dict[WorldRef, tuple[IncrementalFinding, ...]] = {}
    for world, manifest in compilation.manifests().items():
        check = run_check(manifest, profile, registry=registry)
        audit = run_audit(manifest, profile)
        per_world[world] = (*check.findings, *audit.findings)
    return IncrementalWorldCheck(per_world=per_world, compilation=compilation)
