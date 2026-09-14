"""Functional-dependency property: the dependencies a relation's rows satisfy.

A relation's value is a set of functional dependencies over its output column
names, each one ``X -> y``: rows equal on ``X`` are equal on ``y``. This is what
the aggregate coherence guard checks before trusting an aggregate:
``sum(amount) group by country`` over a per-row currency is well typed exactly
when the group key holds the currency constant per group, and a ``country ->
currency`` dependency is the summarizability argument for that (Lenz &
Shoshani, SSDBM 1997; Hurtado & Mendelzon, ICDT 2001). Entailment
(:func:`determines`) is attribute closure under Armstrong's axioms.

The lattice orders by precision exactly as uniqueness orders keys: knowing more
dependencies is more precise, so ``meet`` (resolution of declarations) unions the
sets, ``join`` (confluence) intersects them, ``top`` is the empty set, and
``bottom`` is a formal universal element no real resolution reaches.

Dependencies come from five places. A declaration grounds one directly (synthetic
facts until the authoring bridge lands; the ``determines(...)`` contract is its
eventual source). An equality filter pins a column constant, the empty-determinant
dependency. A GROUP BY makes its group key determine every output (the key of the
grouped result). A candidate key read from the uniqueness property determines
every column selected alongside it, since a relation unique on ``K`` admits one
row per ``K`` value. And a join carries each kept side's dependencies (qualified
by source alias) plus an inner join's ``ON`` equalities as mutual determinations.

A declared dependency is more than a relation fact: it is an axiom about the
declaring relation's world (``order_id determines user_id`` because one order
cannot belong to two users), so it travels as a grounded instance
(:class:`DeclaredFD`) beside the plain set. The distinction pays at a UNION. A
dependency is universally quantified over row pairs, and a union adds exactly the
cross pairs, one row from each arm; a derived dependency's witness is arm-local
(two arms can each pin a column to a different constant), so every derived
dependency dies at the merge, while an instance that every arm carries from one
declaration covers the cross pairs too and survives with its grounding. Posture
elsewhere is silent-when-unproven: an outer join's NULL-padded side proves
nothing, and INTERSECT and EXCEPT claim nothing (each keeps a subset of one arm's
rows, which cannot break a dependency, so carrying the covering arm is a possible
refinement).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from typing import assert_never

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.grounding import grounded_scopes, grounding
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import (
    Annotation,
    CompileValue,
    Declared,
    Fact,
    NativeConstraint,
    Opacity,
)
from dblect.lineage.facts.property import DepContext, Property, PropertyRef, relation_property
from dblect.lineage.graph import SourceRef, source_ref_meta
from dblect.lineage.properties.scope_closure import (
    EMPTY_INPUT,
    FD,
    DeclaredFD,
    Input,
    Key,
    closure,
    scope_facts,
)
from dblect.lineage.properties.uniqueness import CandidateKeySet

# --- the value type ------------------------------------------------------------
#
# ``FD`` and ``DeclaredFD`` live in ``scope_closure.py``: the engine builds and
# carries them directly (they describe one resolved input's own dependencies,
# the shape its projection step produces), and this module imports them back
# for its lattice and public API.


@dataclass(frozen=True, slots=True)
class FDSet:
    """The dependencies a relation is known to satisfy.

    ``fds`` holds every known dependency; the empty set is the lattice ``top``
    ("no dependency known"). ``declared`` holds the grounded instances among them,
    each still tied to its declaration; every instance's current dependency is in
    ``fds`` (enforced at construction), so a consumer reading ``fds`` alone sees
    everything. ``is_bottom`` marks the formal universal element (the lattice
    ``bottom``): it absorbs under ``meet`` and is the identity under ``join``, and
    no resolution of real declarations reaches it, since dependency claims only
    ever union. Equality is structural, so ``FDSet(frozenset())`` (top) and the
    bottom sentinel are distinct values.
    """

    fds: frozenset[FD]
    declared: frozenset[DeclaredFD] = frozenset()
    is_bottom: bool = False

    def __post_init__(self) -> None:
        missing = {inst.fd for inst in self.declared} - self.fds
        if missing:
            raise ValueError(f"declared instances name dependencies outside fds: {missing}")

    @staticmethod
    def of(*fds: FD) -> FDSet:
        return FDSet(frozenset(fds))


# The empty dependency set: "we know of no dependency", the value every
# undeclared relation grounds to and the meet identity.
NO_FDS: FDSet = FDSet(frozenset())

# The formal universal element. Unreachable when resolving real declarations
# (they only union), present so the lattice is bounded.
ALL_FDS: FDSet = FDSet(frozenset(), is_bottom=True)


def _meet(a: FDSet, b: FDSet) -> FDSet:
    """Most precise value consistent with both: the union of the dependencies,
    grounded instances included."""
    if a.is_bottom or b.is_bottom:
        return ALL_FDS
    return FDSet(a.fds | b.fds, a.declared | b.declared)


def _join(a: FDSet, b: FDSet) -> FDSet:
    """Least precise value both refine: the dependencies both sides carry. An
    instance survives only whole (same grounding, same current columns), so the
    same dependency grounded at two different origins keeps neither instance."""
    if a.is_bottom:
        return b
    if b.is_bottom:
        return a
    return FDSet(a.fds & b.fds, a.declared & b.declared)


FUNCTIONAL_DEPENDENCY_LATTICE: Lattice[FDSet] = Lattice(
    meet=_meet,
    join=_join,
    top=NO_FDS,
    bottom=ALL_FDS,
)


def determines(value: FDSet, given: frozenset[str], target: str) -> bool:
    """Whether ``value`` entails ``given -> target``: attribute closure under
    Armstrong's axioms (sound and complete for FD entailment), the same generic
    closure the scope-closure engine runs. The bottom sentinel entails
    everything, and a target inside ``given`` holds by reflexivity."""
    if target in given:
        return True
    if value.is_bottom:
        return True
    pairs = tuple((fd.determinant, fd.dependent) for fd in value.fds)
    return target in closure(pairs, given)


def minimal_cover(value: FDSet, cols: frozenset[str]) -> frozenset[str]:
    """The irreducible subset of ``cols`` that still determines all of ``cols`` under
    ``value``: fold each column into the others whenever they already entail it.

    A join spanning a declared dependency chain (``store_id -> region_id ->
    country_id``) is really keyed on the chain's root, the additional equalities
    functionally redundant. This reduces the spanned columns to that root, so a detector
    reports the declared key as one unit rather than treating co-determined columns as
    independent. With no dependency known (``NO_FDS``) nothing is entailed by the rest,
    so the result is ``cols`` unchanged.

    The fixed-order walk drops a column only when its survivors still entail it, which keeps
    the result irreducible and coverage-preserving; the lattice tests pin both as properties
    over ``determines``."""
    kept = set(cols)
    for col in sorted(cols):
        if determines(value, frozenset(kept - {col}), col):
            kept.discard(col)
    return frozenset(kept)


# --- grounding -----------------------------------------------------------------


def _lift_declared(fact: Fact[FDSet, SourceRef]) -> Fact[FDSet, SourceRef]:
    """Ground a declaration's dependencies as instances carrying its identity.

    A declaration is a person's axiom about the scope's world, so each of its
    dependencies becomes a :class:`DeclaredFD` at that scope. The provenance space
    is closed and each kind is decided explicitly: a native constraint holds by
    the warehouse's write path and a compile value is minted by the toolchain, so
    neither claims a world and neither grounds an instance."""
    match fact.provenance:
        case Declared():
            if fact.value.is_bottom:
                return fact
            instances = frozenset(DeclaredFD.identity(fact.scope, fd) for fd in fact.value.fds)
            return replace(fact, value=FDSet(fact.value.fds, fact.value.declared | instances))
        case NativeConstraint() | CompileValue():
            return fact
    assert_never(fact.provenance)


def _lifted(
    facts: Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]],
) -> Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]]:
    return {scope: tuple(_lift_declared(f) for f in bucket) for scope, bucket in facts.items()}


def functional_dependency_grounding(
    facts: Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]],
    *,
    opaque: Collection[SourceRef] = (),
) -> Callable[[SourceRef], Annotation[FDSet]]:
    """Fold the per-relation dependency facts into grounded annotations. The same
    fold every property uses (an opt-out grounds EXPLICIT top, a resolved bucket
    grounds its value CONCRETE, everything else the IMPLICIT-top default), over
    facts whose declarations are first lifted into grounded instances."""
    return grounding(_lifted(facts), opaque, FUNCTIONAL_DEPENDENCY_LATTICE)


def functional_dependency_grounded_scopes(
    facts: Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]],
    *,
    opaque: Collection[SourceRef] = (),
) -> set[SourceRef]:
    """The relations a dependency fact grounded, for coverage. Reads the same fold
    ``functional_dependency_grounding`` does."""
    return grounded_scopes(_lifted(facts), opaque, FUNCTIONAL_DEPENDENCY_LATTICE)


# --- the property ------------------------------------------------------------


def functional_dependency_property(
    ground: Callable[[SourceRef], Annotation[FDSet]],
    *,
    uniqueness: PropertyRef[CandidateKeySet, SourceRef] | None = None,
) -> Property[FDSet, SourceRef]:
    """The relation-scoped functional-dependency property over a caller-supplied
    grounding (synthetic facts in tests; the contract bridge is the eventual
    source). Declared and inferred dependencies both hold, so they compose by meet
    (``reconcile_by_meet``), exactly as uniqueness composes keys. Passing the
    uniqueness property's ref switches on the key-derived source and declares the
    dependency edge the registry orders by.

    The reducer builds the scope-closure engine's ``Input`` for each base table
    (its dependencies from ``recurse``, its keys from the uniqueness edge when
    it is wired) and reads the engine's projected dependencies back; the engine
    also derives keys, which the uniqueness reducer reads once it carries its
    own walk on the same engine."""

    def reduce_(
        deriv: Expr,
        _prop: Property[FDSet, SourceRef],
        recurse: Callable[[SourceRef], Annotation[FDSet]],
        ctx: DepContext,
        _default: Annotation[FDSet],
        _sink: object = None,
    ) -> Annotation[FDSet]:
        provisional = False

        def base_resolve(table: exp.Table) -> Input:
            nonlocal provisional
            ref = source_ref_meta(table)
            if ref is None:
                return EMPTY_INPUT
            ann = recurse(ref)
            provisional = provisional or ann.provisional
            keys: frozenset[Key] = frozenset()
            if uniqueness is not None:
                key_ann = ctx.annotation(uniqueness, ref)
                if key_ann is not None:
                    keys = key_ann.value.keys
            # The bottom sentinel carries no dependencies to walk with; strip it to
            # the plain sets here, exactly as the walk always has.
            return Input(keys, ann.value.fds, ann.value.declared)

        resolved = scope_facts(deriv, cte_scope={}, base_resolve=base_resolve)
        # A declared instance's own dependency joins the plain set explicitly: the
        # engine may separately derive a strictly stronger simplification (a
        # constant determinant stripped to the empty one), which leaves the
        # instance's own, unsimplified form absent from ``fds`` on its own. Both
        # are sound; ``FDSet`` requires every instance's ``fd`` to be a member.
        fds = resolved.fds | {inst.fd for inst in resolved.declared}
        value = FDSet(fds, resolved.declared)
        opacity = Opacity.CONCRETE if value.fds else Opacity.IMPLICIT
        return Annotation(value, opacity, provisional=provisional)

    return relation_property(
        name="functional_dependency",
        lattice=FUNCTIONAL_DEPENDENCY_LATTICE,
        operators={},
        aggregates={},
        ground=ground,
        depends_on=(uniqueness,) if uniqueness is not None else (),
        reconcile_by_meet=True,
        reducer=reduce_,
    )
