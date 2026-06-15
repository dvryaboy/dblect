"""The flag-world bridge: a hand-declared flag becomes per-world compile facts.

A :class:`DomainFlag` is the hand-authored form of a compile-time flag: its name, the
domain of values to enumerate, and, per value, the fully-refined domain type the
scopes it governs take in that world. The bridge lowers it to the ``CompileFact``\\ s
the enumerator already consumes, so a project gets a cross-world finding from a flag
declared by hand, ahead of the var-inference layer that will discover and scaffold
flags automatically.

Responsiveness (which scopes a flag grounds) is declared directly on the flag for
now. The design's model-responsiveness rule infers it from per-model var usage, which
var-inference produces; until then a hand-declared flag names its responsive scopes,
and the bridge runs unchanged once that inference replaces the hand-declaration.

The world the bridge fixes makes each fact concrete: a flag value is ground truth in
exactly its world, so the fact is a single point, never a disjunction. The
disjunction across values lives in the enumeration over worlds, never in a fact.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from itertools import product

from dblect.check.run import CheckGraphs
from dblect.check.worlds import CompileFact, EnumeratedFindings, TagCompileFact, enumerate_worlds
from dblect.lineage.facts.model import CompileOrigin, CompileValue, Fact, WorldRef
from dblect.lineage.graph import ColumnRef
from dblect.types import DomainType, domain_tag


@dataclass(frozen=True, slots=True)
class DomainFlag:
    """A hand-authored compile-time flag.

    ``affects`` maps each value in the flag's domain to the fully-refined domain type
    the responsive scopes take when the flag holds that value. ``scopes`` are the
    columns the flag governs (its declared responsiveness)."""

    name: str
    affects: Mapping[Hashable, type[DomainType]]
    scopes: tuple[ColumnRef, ...]


def lower_flag(flag: DomainFlag, value: Hashable, world: WorldRef) -> list[TagCompileFact]:
    """The compile facts a flag grounds at one value, in one world: the domain tag the
    value's refined type carries, placed at each responsive scope. A scope whose type
    carries no magnitude (nothing to tag) is skipped."""
    spec = flag.affects[value].spec()
    provenance = CompileValue(origin=CompileOrigin.DBT_VAR, world=world)
    facts: list[TagCompileFact] = []
    for scope in flag.scopes:
        bound = domain_tag(spec, scope.source)
        if bound is not None:
            facts.append(TagCompileFact(Fact(scope=scope, value=bound.tag, provenance=provenance)))
    return facts


def flag_worlds(flags: Sequence[DomainFlag]) -> dict[WorldRef, tuple[CompileFact, ...]]:
    """The worlds the flags induce: the product of their domains, each world carrying
    the compile facts its assignment grounds. With no flags this is the single base
    world (an empty assignment, no facts), so the enumeration degrades to the
    single-world check."""
    worlds: dict[WorldRef, tuple[CompileFact, ...]] = {}
    for combo in product(*(tuple(flag.affects) for flag in flags)):
        assignment = tuple(zip(flags, combo, strict=True))
        world = WorldRef(frozenset((flag.name, value) for flag, value in assignment))
        facts: list[CompileFact] = []
        for flag, value in assignment:
            facts.extend(lower_flag(flag, value, world))
        worlds[world] = tuple(facts)
    return worlds


def check_worlds(graphs: CheckGraphs, flags: Sequence[DomainFlag]) -> EnumeratedFindings:
    """Enumerate the worlds ``flags`` induce and check each over the one shared build.
    The end-to-end entry: declare flags, get per-world findings."""
    return enumerate_worlds(graphs, flag_worlds(flags))
