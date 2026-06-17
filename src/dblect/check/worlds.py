"""The fact-level world enumerator: one shared build, many worlds.

A world is a set of ``CompileValue`` facts layered on top of the manifest's declared
facts. The enumerator holds one ``CheckGraphs`` build and, for each world, grounds
that world's facts over the shared graph and re-propagates, collecting the findings
keyed by the world they hold under. This is the design's "shared walk, only the
leaves vary": the declared facts and the graph are shared, the compile facts are the
per-world leaves.

It covers value-substitution worlds, where the SQL is identical across worlds and
only the grounded values differ. Control-flow worlds, where the SQL itself changes,
need graph patching or the variability-aware front end and are out of scope here.

A finding present in some worlds and absent in others is data, not an error: that is
exactly the "holds under world A, fails under world B" signal the analysis exists to
surface. The enumerator never raises on disagreement.

The enumerator does not cache annotations across worlds. Sharing propagation work
across worlds is the lifted-representation optimization the design defers to the
factoring stream; doing it here would couple worlds that must stay independent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from dblect.check.coverage import WorldCoverage
from dblect.check.findings import CheckFinding
from dblect.check.run import CheckGraphs, WorldFacts, propagate_world, world_findings
from dblect.lineage.facts.model import Fact, WorldRef
from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.properties.domain_type import DomainTag
from dblect.lineage.properties.functional_dependency import FDSet


@dataclass(frozen=True, slots=True)
class TagCompileFact:
    """A per-world domain-type fact (a refinement axis the flag layer sets)."""

    fact: Fact[DomainTag, ColumnRef]


@dataclass(frozen=True, slots=True)
class FdCompileFact:
    """A per-world functional-dependency fact (a key the flag layer grounds)."""

    fact: Fact[FDSet, SourceRef]


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
        """Each finding mapped to the worlds it holds under. A finding whose world
        set is a strict subset of the enumerated worlds is the cross-world signal: it
        failed in those worlds and held in the rest."""
        out: dict[CheckFinding, set[WorldRef]] = {}
        for result in self.per_world:
            for finding in result.findings:
                out.setdefault(finding, set()).add(result.world)
        return {finding: frozenset(worlds) for finding, worlds in out.items()}

    def world_varying(self) -> Mapping[CheckFinding, frozenset[WorldRef]]:
        """The findings that hold in some enumerated worlds but not all, each mapped
        to the worlds it holds under. This is the cross-world signal :meth:`by_finding`
        describes: a finding present in every enumerated world is world-invariant (the
        single-manifest analyzer already reports it) and is excluded here."""
        analyzed = frozenset(result.world for result in self.per_world)
        return {
            finding: worlds for finding, worlds in self.by_finding().items() if worlds != analyzed
        }


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
    ``run_check``'s world-varying findings, which makes base-world identity the
    enumerator's anchoring contract."""
    results: list[WorldResult] = []
    for world, compile_facts in world_facts.items():
        annotations = propagate_world(graphs, _world_facts(graphs, world, compile_facts))
        results.append(
            WorldResult(world=world, findings=tuple(world_findings(graphs, annotations)))
        )
    return EnumeratedFindings(per_world=tuple(results))
