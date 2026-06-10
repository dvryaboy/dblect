"""``Property[K, S]``: a lattice, transfer catalogs, and a grounding function.

A property bundles everything the propagator needs to walk one axis: its
:class:`Lattice`, per-operator and per-aggregate transfers, the ``ground``
function that gives each node its declared :class:`Annotation`, and an optional
:class:`Semiring` for counting or accumulating axes. The transfer calculus and
its obligations are in ``docs/design/propagation-soundness.md``.

A property's typed handle, :class:`PropertyRef`, is minted once by the smart
constructors behind a module-private token, so a caller cannot forge a handle of
the wrong value type and read another property's annotation back mistyped.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, final, runtime_checkable

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Fact, ScopeKind
from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.semiring import Semiring

if TYPE_CHECKING:
    from dblect.manifest import Manifest

K = TypeVar("K")
K2 = TypeVar("K2")
S = TypeVar("S", ColumnRef, SourceRef)
S2 = TypeVar("S2", ColumnRef, SourceRef)

# Module-private mint token; only this module can mint a PropertyRef, so the
# typed dependency read cannot be subverted by a hand-built handle.
_MINT = object()


@final
@dataclass(frozen=True, slots=True)
class PropertyRef(Generic[K2, S2]):
    """A typed handle to a property, minted once as a property's own ``ref``.

    ``K2`` and ``S2`` are the property's real value and scope types, so a read
    site recovers them rather than ``object``. The handle is un-forgeable: its
    constructor requires the module-private mint token, so a caller cannot build
    a ``PropertyRef[WrongK, S]`` with chosen parameters. Equality is on ``name``
    (the registry rejects duplicates); the registry additionally checks a
    ``depends_on`` edge against the *identity* of a registered property's minted
    ref, so a forged handle fails assembly rather than silently mistyping a read.
    """

    name: str
    _mint: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TypeError("PropertyRef is minted by a property constructor, not built directly")


class DepContext(Protocol):
    """A read-only view of the annotations computed so far, by another property.

    A transfer reaches a dependency only through this channel, and only for an
    edge it declared in ``depends_on`` (otherwise it cannot name the typed ref).
    A ``None`` return is the silent-dependency case, which a transfer reads as the
    dependency's lattice top.
    """

    def annotation(self, ref: PropertyRef[K2, S2], scope: S2) -> Annotation[K2] | None: ...


# Transfers receive and return annotations, so opacity and the provisional taint
# flow through them. A property with no dependencies ignores the DepContext.
OperatorTransfer = Callable[[Expr, tuple[Annotation[K], ...], DepContext], Annotation[K]]


F = TypeVar("F")


@dataclass(frozen=True, slots=True)
class CoherenceGuard(Generic[K, F]):
    """A precondition an aggregate's meaning rests on: every per-row companion
    column the aggregated value references must be constant within each group.

    ``companions`` reads those columns off the value being aggregated (a per-row
    currency binding on a money amount). The propagator discharges each one
    against the :class:`~dblect.lineage.graph.AggregationSite` the builder stamped
    on the aggregate call: membership in the group key, a literal pin in the
    aggregating scope's own WHERE, or an ``entails`` read of the ``fd`` dependency
    at the aggregation input (antecedent: the group columns, in the input
    relation's names). Where no path discharges a companion, the aggregate clears
    to top and the seam rule reports it. See ``propagation-soundness.md``.

    ``fd`` must appear in the carrying property's ``depends_on``, so the registry
    orders the dependency first and the read is never silently unevaluated."""

    fd: PropertyRef[F, SourceRef]
    companions: Callable[[K], Collection[ColumnRef]]
    entails: Callable[[F, frozenset[str], str], bool]


@dataclass(frozen=True, slots=True)
class AggregateRule(Generic[K]):
    """An aggregate transfer split so its soundness obligation stays checkable:
    ``core`` is a pure value-domain map (no DepContext), and ``coherence`` is the
    optional clear-on-failure guard through which any dependency enters."""

    core: Callable[[exp.AggFunc, Annotation[K]], Annotation[K]]
    coherence: CoherenceGuard[K, Any] | None = None


@dataclass(frozen=True, slots=True)
class AxisDisplay:
    """The human-facing names the seam diagnostic fills its template from. The
    types layer supplies it from a declaration, with fallback to the bare type and
    axis names."""

    name: str
    description: str | None = None


@runtime_checkable
class FactDiscoverer(Protocol[K, S]):
    """Reads the manifest and dblect declarations, returns facts for any node it
    can ground. Pure, and it returns a materialized collection so a discoverer
    that raises drops all of its facts and none of another's."""

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[K, S]]: ...


@dataclass(frozen=True, slots=True)
class Property(Generic[K, S]):
    """The lattice plus the transfer catalogs plus the grounding function.

    Build one with :func:`column_property` or :func:`relation_property`, which
    mint ``ref`` and fix ``scope_kind`` to match the scope type. ``semiring`` is
    set only for a property whose confluence or cross counts or accumulates; when
    set, the relational operators derive from it and must not be redefined in
    ``operators``.
    """

    ref: PropertyRef[K, S]
    scope_kind: ScopeKind
    lattice: Lattice[K]
    operators: Mapping[type[Expr], OperatorTransfer[K]]
    aggregates: Mapping[type[exp.AggFunc], AggregateRule[K]]
    ground: Callable[[S], Annotation[K]]
    semiring: Semiring[K] | None = None
    display: Callable[[K], AxisDisplay] | None = None
    depends_on: tuple[PropertyRef[Any, Any], ...] = ()
    reconcile_by_meet: bool = False
    """How a derived node's declared and inferred annotations combine.

    Default (``False``): an inferred value that fails ``consistent`` against the
    declaration is a conflict; the flow value keeps the declaration, tainted
    provisional (nullability: a declared ``NOT NULL`` the SQL can violate). Set
    (``True``): declared and inferred are the same-polarity lower bounds and
    compose by the lattice ``meet``, never conflicting (uniqueness: a declared
    candidate key and a SQL-derived one both hold, so they union)."""
    reducer: Reducer | None = None
    """The scope-specific step that turns a derivation into an inferred annotation.

    ``None`` lets the propagator supply its default for the scope kind. Column
    reduction is generic (it dispatches to this property's own operators,
    aggregates, semiring, and lattice), so a column property leaves this unset.
    Relation reduction is property-specific today (a relation-algebra walk has no
    per-property transfer protocol yet), so a relation property carries its walk
    here rather than relying on a global registry."""

    def __post_init__(self) -> None:
        # A semiring-carrying property derives its confluence and cross from
        # plus/times, so it must not also pin those operators by hand. The semiring
        # laws themselves are PBT obligations (see propagation-soundness.md), not
        # decidable here.
        if self.semiring is not None:
            clash = {exp.Union, exp.Join} & set(self.operators)
            if clash:
                names = ", ".join(sorted(c.__name__ for c in clash))
                raise ValueError(
                    f"property {self.ref.name!r} carries a semiring, so {names} must not "
                    "be redefined in operators; the relational combine derives from the semiring"
                )
        # A coherence guard reads its fd through the DepContext; that read is only
        # ordered (and the handle only verified against a registered property) when
        # the edge is declared, so an undeclared guard dependency is a construction
        # error here rather than a silently-unevaluated read at propagation time.
        undeclared = sorted(
            rule.coherence.fd.name
            for rule in self.aggregates.values()
            if rule.coherence is not None and rule.coherence.fd not in self.depends_on
        )
        if undeclared:
            raise ValueError(
                f"property {self.ref.name!r} carries a coherence guard reading "
                f"{', '.join(undeclared)}, which is not declared in depends_on"
            )

    @property
    def name(self) -> str:
        return self.ref.name


# A reducer turns one node's derivation into an inferred annotation, calling
# ``recurse`` to pull in the annotation of any node the derivation references.
# It is the *only* scope-specific step of propagation; grounding, reconcile, the
# cycle guard, and memoisation are shared. A relation property carries its reducer
# on ``Property.reducer``; column scope uses the propagator's generic default. The
# signature erases to ``Any`` because the propagator dispatches it dynamically
# while keeping its public ``propagate`` precisely typed.
Reducer = Callable[
    [Expr, Property[Any, Any], Callable[[Any], Annotation[Any]], DepContext, Annotation[Any]],
    Annotation[Any],
]


def column_property(
    *,
    name: str,
    lattice: Lattice[K],
    operators: Mapping[type[Expr], OperatorTransfer[K]],
    aggregates: Mapping[type[exp.AggFunc], AggregateRule[K]],
    ground: Callable[[ColumnRef], Annotation[K]],
    semiring: Semiring[K] | None = None,
    display: Callable[[K], AxisDisplay] | None = None,
    depends_on: tuple[PropertyRef[Any, Any], ...] = (),
    reconcile_by_meet: bool = False,
) -> Property[K, ColumnRef]:
    """Mint a column-scoped property: ``scope_kind`` is COLUMN and facts address columns."""
    return Property(
        ref=PropertyRef(name=name, _mint=_MINT),
        scope_kind=ScopeKind.COLUMN,
        lattice=lattice,
        operators=operators,
        aggregates=aggregates,
        ground=ground,
        semiring=semiring,
        display=display,
        depends_on=depends_on,
        reconcile_by_meet=reconcile_by_meet,
    )  # column scope leaves reducer unset: the propagator's generic reducer serves it.


def relation_property(
    *,
    name: str,
    lattice: Lattice[K],
    operators: Mapping[type[Expr], OperatorTransfer[K]],
    aggregates: Mapping[type[exp.AggFunc], AggregateRule[K]],
    ground: Callable[[SourceRef], Annotation[K]],
    semiring: Semiring[K] | None = None,
    display: Callable[[K], AxisDisplay] | None = None,
    depends_on: tuple[PropertyRef[Any, Any], ...] = (),
    reconcile_by_meet: bool = False,
    reducer: Reducer | None = None,
) -> Property[K, SourceRef]:
    """Mint a relation-scoped property: ``scope_kind`` is RELATION and facts address relations.

    A relation property must supply ``reducer`` (its relation-algebra walk);
    propagation raises if it is left unset, since relation reduction has no
    generic default the way column reduction does."""
    return Property(
        ref=PropertyRef(name=name, _mint=_MINT),
        scope_kind=ScopeKind.RELATION,
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
