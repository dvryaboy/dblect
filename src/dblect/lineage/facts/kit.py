"""The property kit: the shared scaffolding a manifest-backed property repeats.

Every property that grounds from dbt-declared facts needs the same handful of
pieces, whatever it means: a facts collector that runs its discoverers and folds
in anything a caller already resolved, a grounding fold and its grounded-scopes
coverage reader, a conflicts scan, and the call into
:func:`~dblect.lineage.facts.property.column_property` /
:func:`~dblect.lineage.facts.property.relation_property` itself. Those two stay
the underlying minting functions; this module derives the rest from the lattice,
so a property module supplies only its discoverers, its transfer rules, and
whatever makes it different from a plain fold (a semiring, a coherence guard, a
relation reducer).

``grounding_fold`` is the smaller half, for a property that takes its ``ground``
from a caller-supplied fact map rather than reading the manifest directly
(domain-type and functional-dependency today: the typed contract bridge resolves
their facts elsewhere). ``column_kit`` / ``relation_kit`` are the fuller
constructor, for a property whose discoverers read the manifest themselves
(nullability, uniqueness).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.grounding import (
    collect,
    conflicting_scopes,
    grounded_scopes,
    grounding,
)
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Fact, Opacity
from dblect.lineage.facts.property import (
    AggregateRule,
    AxisDisplay,
    DepContext,
    FactDiscoverer,
    OperatorTransfer,
    Property,
    PropertyRef,
    Reducer,
    column_property,
    relation_property,
)
from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.semiring import Semiring

if TYPE_CHECKING:
    from dblect.manifest import Manifest

K = TypeVar("K")
S = TypeVar("S", ColumnRef, SourceRef)


def top_rule(lat: Lattice[K]) -> OperatorTransfer[K]:
    """The catch-all transfer for a node this property makes no claim about: the
    lattice top, IMPLICIT (nothing declared), with ``provisional`` carried through
    from the children.

    Bind it to whichever node types a property wants to force to top rather than
    fold through the default child combine: a boolean-yielding comparison whose
    operands' tags say nothing about the comparison's own type, for instance."""

    def rule(_expr: Expr, kids: tuple[Annotation[K], ...], _ctx: DepContext) -> Annotation[K]:
        return Annotation(lat.top, Opacity.IMPLICIT, provisional=any(k.provisional for k in kids))

    return rule


def constant_aggregate(value: K, *, opacity: Opacity = Opacity.CONCRETE) -> AggregateRule[K]:
    """An aggregate whose meaning discards its child's value entirely and always
    reduces to ``value``.

    ``COUNT`` is the recurring case: safe over anything, it reports a cardinality
    whatever the child carried (nullability's ``NON_NULL``, domain-type's
    tag-free ``NAKED``, a closed-value-set property's unbounded top). The default
    opacity is ``CONCRETE`` for a positive structural claim like ``NON_NULL``;
    pass ``Opacity.IMPLICIT`` when ``value`` is the lattice top and the aggregate
    is really saying "no information", not "no claim". ``provisional`` still
    rides through, since a genuinely broken input should still taint the result.
    """

    def core(_expr: exp.AggFunc, child: Annotation[K]) -> Annotation[K]:
        return Annotation(value, opacity, provisional=child.provisional)

    return AggregateRule(core=core)


@dataclass(frozen=True, slots=True)
class GroundingFold(Generic[K, S]):
    """A grounding fold and its grounded-scopes/conflicts readers, bound once to a
    lattice and an optional fact preprocessor.

    ``preprocess`` is the one hook a property passes explicitly when its facts
    need a step before the plain fold: functional-dependency lifts a declaration's
    dependencies into grounded instances first. Everything else takes the facts
    exactly as collected.

    Bind with :func:`grounding_fold`, annotating the target with the property's
    own ``K``/``S`` (``GroundingFold[DomainTag, ColumnRef]``): neither type
    parameter appears in ``grounding_fold``'s own arguments, so the annotation is
    what lets it infer them rather than falling back to ``Unknown``."""

    lattice: Lattice[K]
    preprocess: Callable[[Mapping[S, tuple[Fact[K, S], ...]]], Mapping[S, tuple[Fact[K, S], ...]]]

    def ground(
        self, facts: Mapping[S, tuple[Fact[K, S], ...]], *, opaque: Collection[S] = ()
    ) -> Callable[[S], Annotation[K]]:
        return grounding(self.preprocess(facts), opaque, self.lattice)

    def scopes(
        self, facts: Mapping[S, tuple[Fact[K, S], ...]], *, opaque: Collection[S] = ()
    ) -> set[S]:
        return grounded_scopes(self.preprocess(facts), opaque, self.lattice)

    def conflicts(self, facts: Mapping[S, tuple[Fact[K, S], ...]]) -> tuple[S, ...]:
        return conflicting_scopes(self.preprocess(facts), self.lattice)


def _identity(facts: Mapping[S, tuple[Fact[K, S], ...]]) -> Mapping[S, tuple[Fact[K, S], ...]]:
    return facts


def grounding_fold(
    lattice: Lattice[K],
    *,
    preprocess: Callable[[Mapping[S, tuple[Fact[K, S], ...]]], Mapping[S, tuple[Fact[K, S], ...]]]
    | None = None,
) -> GroundingFold[K, S]:
    """Bind the shared grounding fold to ``lattice``, for a property whose
    ``ground`` a caller supplies from a fact map it already has (rather than one
    this module collects from the manifest via discoverers). The property module
    binds this once and exposes ``.ground``/``.scopes`` under its own names, so
    the wrapper stays a named, importable function without a hand-written body.

    ``S`` is not pinned by any argument here (a bare ``Lattice[K]`` says nothing
    about the scope type), so annotate the assignment target
    (``GroundingFold[DomainTag, ColumnRef]``) rather than relying on inference."""
    return GroundingFold(lattice=lattice, preprocess=preprocess or _identity)


@dataclass(frozen=True, slots=True)
class PropertyKit(Generic[K, S]):
    """The derived surface above ``column_property``/``relation_property`` for a
    property that grounds from manifest discoverers: a facts collector, the
    grounding fold (and its grounded-scopes/conflicts readers), and the property
    constructor itself, all bound once to the lattice and transfer rules.

    Built with :func:`column_kit` or :func:`relation_kit`, whose own return type
    pins ``S`` to ``ColumnRef``/``SourceRef``. A property whose grounding needs
    more than the plain fold (uniqueness's carried conditional keys) uses
    ``.facts`` for the collector and builds its ``Property`` by hand with its own
    ground function, exactly as it would without the kit."""

    fold: GroundingFold[K, S]
    _build: Callable[[Callable[[S], Annotation[K]]], Property[K, S]]

    def facts(
        self,
        manifest: Manifest,
        discoverers: tuple[FactDiscoverer[K, S], ...],
        *,
        name_to_source: Mapping[str, SourceRef] = {},
        extra_facts: tuple[Fact[K, S], ...] = (),
    ) -> Mapping[S, tuple[Fact[K, S], ...]]:
        """Run ``discoverers`` over ``manifest`` and fold in ``extra_facts`` a
        caller already resolved (a Python contract), the one collector every
        manifest-backed property calls."""
        return collect(
            manifest, discoverers, name_to_source=name_to_source, extra_facts=extra_facts
        )

    def grounding(
        self, facts: Mapping[S, tuple[Fact[K, S], ...]], *, opaque: Collection[S] = ()
    ) -> Callable[[S], Annotation[K]]:
        return self.fold.ground(facts, opaque=opaque)

    def grounded_scopes(
        self, facts: Mapping[S, tuple[Fact[K, S], ...]], *, opaque: Collection[S] = ()
    ) -> set[S]:
        return self.fold.scopes(facts, opaque=opaque)

    def conflicts(self, facts: Mapping[S, tuple[Fact[K, S], ...]]) -> tuple[S, ...]:
        return self.fold.conflicts(facts)

    def property(
        self, facts: Mapping[S, tuple[Fact[K, S], ...]], *, opaque: Collection[S] = ()
    ) -> Property[K, S]:
        return self._build(self.grounding(facts, opaque=opaque))


def column_kit(
    *,
    name: str,
    lattice: Lattice[K],
    operators: Mapping[type[Expr], OperatorTransfer[K]],
    aggregates: Mapping[type[exp.AggFunc], AggregateRule[K]],
    column_meta: Mapping[str, OperatorTransfer[K]] | None = None,
    semiring: Semiring[K] | None = None,
    display: Callable[[K], AxisDisplay] | None = None,
    depends_on: tuple[PropertyRef[Any, Any], ...] = (),
    reconcile_by_meet: bool = False,
) -> PropertyKit[K, ColumnRef]:
    """A :class:`PropertyKit` for a column-scoped property, fixing every transfer
    rule up front so the kit's ``.property`` needs only a ``ground`` function."""

    def build(ground: Callable[[ColumnRef], Annotation[K]]) -> Property[K, ColumnRef]:
        return column_property(
            name=name,
            lattice=lattice,
            operators=operators,
            aggregates=aggregates,
            ground=ground,
            column_meta=column_meta,
            semiring=semiring,
            display=display,
            depends_on=depends_on,
            reconcile_by_meet=reconcile_by_meet,
        )

    fold: GroundingFold[K, ColumnRef] = grounding_fold(lattice)
    return PropertyKit(fold=fold, _build=build)


def relation_kit(
    *,
    name: str,
    lattice: Lattice[K],
    operators: Mapping[type[Expr], OperatorTransfer[K]],
    aggregates: Mapping[type[exp.AggFunc], AggregateRule[K]],
    semiring: Semiring[K] | None = None,
    display: Callable[[K], AxisDisplay] | None = None,
    depends_on: tuple[PropertyRef[Any, Any], ...] = (),
    reconcile_by_meet: bool = False,
    reducer: Reducer | None = None,
) -> PropertyKit[K, SourceRef]:
    """A :class:`PropertyKit` for a relation-scoped property. ``reducer`` is
    almost always needed (relation reduction has no generic default), but is kept
    optional here so the construction error stays at propagation time, matching
    :func:`~dblect.lineage.facts.property.relation_property`."""

    def build(ground: Callable[[SourceRef], Annotation[K]]) -> Property[K, SourceRef]:
        return relation_property(
            name=name,
            lattice=lattice,
            operators=operators,
            aggregates=aggregates,
            ground=ground,
            semiring=semiring,
            display=display,
            depends_on=depends_on,
            reconcile_by_meet=reconcile_by_meet,
            reducer=reducer,
        )

    fold: GroundingFold[K, SourceRef] = grounding_fold(lattice)
    return PropertyKit(fold=fold, _build=build)
