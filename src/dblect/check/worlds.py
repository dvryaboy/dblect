"""The fact-level world enumerator: one shared build, many worlds.

A world is a set of ``CompileValue`` facts (say, one value a dbt var could take)
layered on top of the manifest's declared facts. The enumerator builds the lineage
graphs once and, for each world, grounds that world's facts over the shared graph
and re-propagates, collecting the findings keyed by the world they hold under. The
graph and the declared facts are shared across worlds; only each world's own facts
change.

It covers worlds where the SQL text is identical and only a substituted value
differs. A world where the SQL itself branches, such as a dbt incremental model's
full-refresh versus steady-state compile, needs a different mechanism (see
:mod:`dblect.check.incremental`) and is out of scope here.

A finding present in some worlds and absent in others is exactly the signal this
analysis exists to surface: the issue holds under one configuration and not
another. The enumerator reports that as data and never raises on the disagreement.

The enumerator re-propagates from scratch for every world rather than reusing work
across them. Sharing propagation results across worlds is a real optimization for
later, deferred because doing it now would let worlds that must stay independent
leak into each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from dblect.check.coverage import WorldCoverage
from dblect.check.findings import CheckFinding
from dblect.check.run import (
    CheckGraphs,
    WorldFacts,
    propagate_world,
    suppress_check_findings,
    world_findings,
)
from dblect.lineage.facts.model import CompileValue, Fact, Provenance, WorldRef
from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.properties.domain_type import DomainTag
from dblect.lineage.properties.functional_dependency import FDSet


def _require_compile_value(provenance: Provenance) -> None:
    """A compile fact carries only a toolchain-minted value. Any other provenance
    routed through this seam would be re-grounded as if declared; for FDs that
    would lift a flag-pinned value into a world axiom that survives unions."""
    if not isinstance(provenance, CompileValue):
        raise ValueError(f"a compile fact requires CompileValue provenance, got {provenance}")


@dataclass(frozen=True, slots=True)
class TagCompileFact:
    """A per-world fact about a column's domain type: what a flag value pins the
    column's type to in that world."""

    fact: Fact[DomainTag, ColumnRef]

    def __post_init__(self) -> None:
        _require_compile_value(self.fact.provenance)


@dataclass(frozen=True, slots=True)
class FdCompileFact:
    """A per-world fact about a relation's functional-dependency key: what a flag
    value pins the key to in that world."""

    fact: Fact[FDSet, SourceRef]

    def __post_init__(self) -> None:
        _require_compile_value(self.fact.provenance)


# A per-world fact, tagged by the property it grounds so the enumerator routes it
# into the right WorldFacts bucket without inspecting value types. The bridge will
# produce these from a flag's ``affects`` clause; for now a caller supplies them.
CompileFact = TagCompileFact | FdCompileFact


@dataclass(frozen=True, slots=True)
class WorldResult:
    """One world's findings, keyed back to the world they hold under."""

    world: WorldRef
    findings: tuple[CheckFinding, ...]


@dataclass(frozen=True, slots=True)
class EnumeratedFindings:
    """Findings aggregated across the enumerated worlds."""

    per_world: tuple[WorldResult, ...]

    def coverage(self) -> WorldCoverage:
        """The world coverage this enumeration achieved: the world count and the flag
        axes swept."""
        return WorldCoverage.over(result.world for result in self.per_world)

    def by_finding(self) -> Mapping[CheckFinding, frozenset[WorldRef]]:
        """Each finding mapped to the worlds it fired in. When that set is a strict
        subset of all enumerated worlds, that's the cross-world signal: whatever the
        finding flags broke in those worlds while working fine in the rest."""
        out: dict[CheckFinding, set[WorldRef]] = {}
        for result in self.per_world:
            for finding in result.findings:
                out.setdefault(finding, set()).add(result.world)
        return {finding: frozenset(worlds) for finding, worlds in out.items()}


def _world_facts(
    graphs: CheckGraphs, world: WorldRef, compile_facts: tuple[CompileFact, ...]
) -> WorldFacts:
    """The declared facts (shared across worlds) with this world's compile facts
    appended, routed by the property each grounds."""
    tag_facts = list(graphs.resolved.tag_facts)
    fd_facts = list(graphs.resolved.fd_facts)
    for compile_fact in compile_facts:
        if isinstance(compile_fact, TagCompileFact):
            tag_facts.append(compile_fact.fact)
        else:
            fd_facts.append(compile_fact.fact)
    return WorldFacts(world=world, fd_facts=tuple(fd_facts), tag_facts=tuple(tag_facts))


def enumerate_worlds(
    graphs: CheckGraphs,
    world_facts: Mapping[WorldRef, tuple[CompileFact, ...]],
) -> EnumeratedFindings:
    """Propagate each world's facts over the one shared ``graphs`` build and collect
    the world-varying findings per world. Results follow ``world_facts`` iteration
    order, so a caller that passes an ordered mapping gets a deterministic report.

    A world carrying no compile facts (``BASE_WORLD`` mapped to ``()``) reproduces
    ``run_check``'s world-varying findings exactly; keeping that agreement is what
    verifies the enumerator has not drifted from the single-world check it
    generalizes."""
    results: list[WorldResult] = []
    for world, compile_facts in world_facts.items():
        annotations = propagate_world(graphs, _world_facts(graphs, world, compile_facts))
        # Apply -- noqa the same way single-world run_check does, so a triaged finding
        # stays silenced here rather than reappearing as active in every world.
        active, _ = suppress_check_findings(world_findings(graphs, annotations), graphs.manifest)
        results.append(WorldResult(world=world, findings=active))
    return EnumeratedFindings(per_world=tuple(results))
