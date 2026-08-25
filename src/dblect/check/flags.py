"""Turning a hand-declared flag into the per-world facts the checker enumerates over.

A :class:`DomainFlag` is how a user tells dblect about a dbt var (or similar
compile-time flag) that changes a column's domain type depending on its value: the
flag's name, the values it can take, and for each value, the type the columns it
governs should carry when that value is set. This module lowers a flag into the
``CompileFact``\\ s the world enumerator already consumes, one set per value, so a
project gets a cross-configuration finding from a flag declared by hand, ahead of
future work that discovers such flags automatically from ``var()`` usage.

Which columns a flag governs is named directly on the flag for now (``scopes``); a
later pass is meant to infer that from which models actually read the flag's var,
and this module will not need to change when that lands.

Each world fixes one value per flag, so the fact this module produces for a world
is always a single concrete type, never "one of these values." The uncertainty
across values lives in having several worlds, not in any one fact.
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
    """A hand-declared compile-time flag.

    ``affects`` maps each value the flag can take to the domain type its columns
    should carry when the flag holds that value. ``scopes`` are the columns the
    flag governs."""

    name: str
    affects: Mapping[Hashable, type[DomainType]]
    scopes: tuple[ColumnRef, ...]


def lower_flag(flag: DomainFlag, value: Hashable, world: WorldRef) -> list[TagCompileFact]:
    """The compile facts one flag value implies, in one world: the domain tag the
    value's type carries, placed at each scope the flag governs. A scope whose type
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
    """The worlds the flags induce: one world per combination of flag values, each
    carrying the compile facts that combination produces. With no flags this is just
    the single base world (empty assignment, no facts), so the enumeration degrades
    to the single-world check."""
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
