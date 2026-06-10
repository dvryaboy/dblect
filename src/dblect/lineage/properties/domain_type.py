"""Domain-type property: per-column tagged magnitudes (the currency story).

A magnitude column (``amount``) carries a :class:`DomainTag`: a dimensional
monomial (currency and units, which do exponent arithmetic under ``*`` and ``/``)
plus categorical nominal tags (``contains_tax``, ``country``, carried by equality
only). Each tag binds either to a literal (a pinned currency) or to a companion
column travelling with the amount (a per-row currency column). This is the
multi-column companion binding the substrate-readiness notes name as the first
genuine build over the lineage engine: the property value is structured, so the
work is a lattice and transfer rules rather than engine plumbing.

The lattice orders by tag knowledge. ``NAKED`` (no tag) is the top, the
freely-summable magnitude making no claim; a known tagging refines it; two known
taggings that disagree meet to ``CONFLICT``, the bottom (``MoneyUSD`` added to
``MoneyEUR``). ``meet`` unions agreeing tags and conflicts on disagreement;
``join`` keeps only the tags both sides agree on, widening the rest back to
``NAKED``. The algebra is read off the field types the author declares, following
the dimension-type tradition (Kennedy, *Dimension Types*, ESOP 1994) and the
summarizability story (Lenz & Shoshani, SSDBM 1997); see
``docs/design/domain-type-algebra.md``.

Naked-amount taint falls out of lineage: when ``amount`` flows to a model where
its companion ``currency`` column was projected away, the binding rides as a
reference that no longer agrees at a confluence and widens to ``NAKED``; the
coherence guard then blocks a downstream sum until a dependency discharges it.
The guard is armed by :func:`domain_type_property` when the caller passes the
functional-dependency property's ref.

Grounding for the first version comes from synthetic facts supplied by a caller
(the same way the uniqueness and nullability tests ground their properties); the
typed source is the contract bridge in the authoring layer.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from functools import reduce
from typing import Final, final

from sqlglot import Expr
from sqlglot import expressions as exp

from dblect.lineage.facts.grounding import grounding
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Fact, Opacity
from dblect.lineage.facts.property import (
    AggregateRule,
    CoherenceGuard,
    DepContext,
    OperatorTransfer,
    Property,
    PropertyRef,
    column_property,
)
from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.properties.functional_dependency import FDSet, determines

# --- unit and tag identities -------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class Concrete:
    """A pinned unit or category identity: a literal currency ``"usd"``, a literal
    ``contains_tax`` value. Case-folded so identities line up the way the graph
    folds column names."""

    name: str


@final
@dataclass(frozen=True, slots=True)
class PerRow:
    """A per-row identity: the companion column travelling with the magnitude (the
    ``currency`` column a ``Money`` amount references). Two amounts share a unit
    only when they reference the *same* column, which is how ``PerRow(c) / PerRow(c)``
    cancels while two different currency columns do not."""

    column: ColumnRef


# The identity of a unit (dimensional) or a nominal category binding. A dimensional
# tag rides as a ``Unit`` exponent; a nominal tag rides as a ``Unit`` value under its
# tag name. Both share the literal-or-companion shape.
Unit = Concrete | PerRow


# --- the dimensional monomial ------------------------------------------------


def _drop_zeros(items: Mapping[Unit, int]) -> frozenset[tuple[Unit, int]]:
    return frozenset((u, e) for u, e in items.items() if e != 0)


@final
@dataclass(frozen=True, slots=True)
class Dimension:
    """A monomial in the free abelian group over units: each unit to a nonzero
    integer exponent, zero exponents dropped, so the empty map is dimensionless and
    equality is content equality. ``money`` is ``{usd: 1}``, ``money^2`` is
    ``{usd: 2}``, an exchange rate is ``{eur: 1, usd: -1}``, a ratio that cancelled
    is ``{}``. The operations are the group operations: ``*`` adds exponents, ``/``
    subtracts them."""

    exponents: frozenset[tuple[Unit, int]]

    @staticmethod
    def dimensionless() -> Dimension:
        return Dimension(frozenset())

    @staticmethod
    def of(unit: Unit, power: int = 1) -> Dimension:
        return Dimension(_drop_zeros({unit: power}))

    @property
    def is_dimensionless(self) -> bool:
        return not self.exponents

    def _map(self) -> dict[Unit, int]:
        return dict(self.exponents)

    def multiply(self, other: Dimension) -> Dimension:
        merged = self._map()
        for unit, power in other.exponents:
            merged[unit] = merged.get(unit, 0) + power
        return Dimension(_drop_zeros(merged))

    def divide(self, other: Dimension) -> Dimension:
        merged = self._map()
        for unit, power in other.exponents:
            merged[unit] = merged.get(unit, 0) - power
        return Dimension(_drop_zeros(merged))


# --- nominal categorical tags ------------------------------------------------

# A nominal tag binds to a literal (``Concrete``) or a companion column (``PerRow``),
# carried by equality only: there is no ``contains_tax^2``.
Nominal = Concrete | PerRow


# --- the per-column value ----------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class Tagged:
    """A known domain tagging on a magnitude column.

    ``dimension`` is the dimensional monomial (currency and units), or ``None`` when
    the column makes no dimensional claim (a plain magnitude, or one whose dimension
    widened away at a confluence). ``nominal`` holds the categorical bindings keyed
    by tag name. ``NAKED`` is ``Tagged(None, {})``, the lattice top.
    """

    dimension: Dimension | None
    nominal: frozenset[tuple[str, Nominal]]

    def nominal_map(self) -> dict[str, Nominal]:
        return dict(self.nominal)


@final
@dataclass(frozen=True, slots=True)
class _Conflict:
    """The lattice bottom: two known taggings that no value can satisfy at once
    (``MoneyUSD`` met with ``MoneyEUR``). A singleton; carries no payload."""


CONFLICT: Final[_Conflict] = _Conflict()

# A column's domain tag is either a known tagging or the conflict bottom.
DomainTag = Tagged | _Conflict

# The lattice top: a magnitude with no tag, freely summable, making no claim.
NAKED: Final[DomainTag] = Tagged(dimension=None, nominal=frozenset())


def _froze(nominal: Mapping[str, Nominal]) -> frozenset[tuple[str, Nominal]]:
    return frozenset(nominal.items())


def tagged(*, dimension: Dimension | None = None, nominal: Mapping[str, Nominal] = {}) -> Tagged:
    """Build a :class:`Tagged` from a dimension and a nominal mapping. The public
    constructor callers use so the frozenset packing of ``nominal`` stays in one place."""
    return Tagged(dimension=dimension, nominal=_froze(nominal))


# --- the lattice -------------------------------------------------------------


def _meet_dimension(a: Dimension | None, b: Dimension | None) -> Dimension | None | _Conflict:
    """``None`` is the no-claim identity. Two known, equal monomials agree; two known,
    unequal monomials are a contradiction on the same column."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a == b else CONFLICT


def _meet_nominal(a: Mapping[str, Nominal], b: Mapping[str, Nominal]) -> dict[str, Nominal] | None:
    """Union the bindings; a tag both sides carry must agree, else the meet conflicts
    (``None``). A tag only one side carries is taken on (compose)."""
    merged = dict(a)
    for name, binding in b.items():
        existing = merged.get(name)
        if existing is not None and existing != binding:
            return None
        merged[name] = binding
    return merged


def _meet(a: DomainTag, b: DomainTag) -> DomainTag:
    if isinstance(a, _Conflict) or isinstance(b, _Conflict):
        return CONFLICT
    dim = _meet_dimension(a.dimension, b.dimension)
    if isinstance(dim, _Conflict):
        return CONFLICT
    nominal = _meet_nominal(a.nominal_map(), b.nominal_map())
    if nominal is None:
        return CONFLICT
    return Tagged(dim, _froze(nominal))


def _join(a: DomainTag, b: DomainTag) -> DomainTag:
    if isinstance(a, _Conflict):
        return b
    if isinstance(b, _Conflict):
        return a
    dim = a.dimension if a.dimension == b.dimension else None
    bm = b.nominal_map()
    agreeing = {
        name: binding for name, binding in a.nominal_map().items() if bm.get(name) == binding
    }
    return Tagged(dim, _froze(agreeing))


DOMAIN_TYPE_LATTICE: Final[Lattice[DomainTag]] = Lattice(
    meet=_meet,
    join=_join,
    top=NAKED,
    bottom=CONFLICT,
)


# --- transfer helpers --------------------------------------------------------


def _annotate(value: DomainTag, kids: tuple[Annotation[DomainTag], ...]) -> Annotation[DomainTag]:
    """Wrap a transfer's result value with the diagnostic bits derived from its inputs:
    a non-top result is CONCRETE, a top result inherits EXPLICIT from a declared opt-out
    or is IMPLICIT, and provisional is the OR of the inputs."""
    provisional = any(k.provisional for k in kids)
    if value != NAKED:
        return Annotation(value, Opacity.CONCRETE, provisional=provisional)
    explicit = any(k.opacity is Opacity.EXPLICIT for k in kids)
    opacity = Opacity.EXPLICIT if explicit else Opacity.IMPLICIT
    return Annotation(value, opacity, provisional=provisional)


# --- operator transfers ------------------------------------------------------


def _additive_rule(
    _expr: Expr, kids: tuple[Annotation[DomainTag], ...], _ctx: DepContext
) -> Annotation[DomainTag]:
    """``+`` and ``-`` require every tag to agree (the same currency, the same
    categories), producing that tag. Disagreement meets to ``CONFLICT``, which a
    detector reads as a mixed-magnitude addition. A naked operand carries no claim,
    so it composes onto the other side rather than tainting it."""
    if not kids:
        return Annotation(NAKED, Opacity.IMPLICIT)
    value = reduce(_meet, (k.value for k in kids))
    return _annotate(value, kids)


def _multiply_tags(a: DomainTag, b: DomainTag) -> DomainTag:
    if isinstance(a, _Conflict) or isinstance(b, _Conflict):
        return CONFLICT
    return _dimensional_combine(a, b, multiply=True)


def _divide_tags(a: DomainTag, b: DomainTag) -> DomainTag:
    if isinstance(a, _Conflict) or isinstance(b, _Conflict):
        return CONFLICT
    return _dimensional_combine(a, b, multiply=False)


def _dimensional_combine(a: Tagged, b: Tagged, *, multiply: bool) -> Tagged:
    """The Kennedy multiplicative fragment: dimensions compose by adding (``*``) or
    subtracting (``/``) exponents, treating a no-claim operand as dimensionless so a
    scalar factor leaves a magnitude's currency intact. Two no-claim operands stay
    no-claim rather than minting a spurious dimensionless ratio. A nominal tag rides
    through when only one side carries it and widens away when both do."""
    if a.dimension is None and b.dimension is None:
        dim: Dimension | None = None
    else:
        da = a.dimension if a.dimension is not None else Dimension.dimensionless()
        db = b.dimension if b.dimension is not None else Dimension.dimensionless()
        dim = da.multiply(db) if multiply else da.divide(db)
    am, bm = a.nominal_map(), b.nominal_map()
    nominal = {name: binding for name, binding in am.items() if name not in bm}
    nominal.update({name: binding for name, binding in bm.items() if name not in am})
    return Tagged(dim, _froze(nominal))


def _multiplicative_rule(
    combine: Callable[[DomainTag, DomainTag], DomainTag],
) -> OperatorTransfer[DomainTag]:
    def rule(
        _expr: Expr, kids: tuple[Annotation[DomainTag], ...], _ctx: DepContext
    ) -> Annotation[DomainTag]:
        if not kids:
            return Annotation(NAKED, Opacity.IMPLICIT)
        value = reduce(combine, (k.value for k in kids))
        return _annotate(value, kids)

    return rule


def _comparison_rule(
    _expr: Expr, kids: tuple[Annotation[DomainTag], ...], _ctx: DepContext
) -> Annotation[DomainTag]:
    """A comparison yields a boolean, which carries no magnitude tag. The operands'
    tags must agree for the comparison to mean anything, but that obligation is a
    seam concern; the produced value is always tag-free."""
    return Annotation(NAKED, Opacity.IMPLICIT, provisional=any(k.provisional for k in kids))


DOMAIN_TYPE_OPERATORS: Mapping[type[Expr], OperatorTransfer[DomainTag]] = {
    exp.Add: _additive_rule,
    exp.Sub: _additive_rule,
    exp.Mul: _multiplicative_rule(_multiply_tags),
    exp.Div: _multiplicative_rule(_divide_tags),
    exp.EQ: _comparison_rule,
    exp.NEQ: _comparison_rule,
    exp.LT: _comparison_rule,
    exp.LTE: _comparison_rule,
    exp.GT: _comparison_rule,
    exp.GTE: _comparison_rule,
}


# --- aggregate transfers -----------------------------------------------------


def _passthrough_core(_expr: exp.AggFunc, child: Annotation[DomainTag]) -> Annotation[DomainTag]:
    """``sum`` and ``avg`` accumulate the magnitude, ``min``/``max`` select one of its
    values; either way the result carries the child's tag. Whether the accumulation is
    *sound* (the tag constant per group) is the coherence guard's obligation, wired
    separately; the pure value-domain map keeps the tag."""
    return child


def _count_core(_expr: exp.AggFunc, child: Annotation[DomainTag]) -> Annotation[DomainTag]:
    """``count`` does not inspect values, so it is always safe and yields a tag-free
    ``Count`` whatever the child's tag."""
    return Annotation(NAKED, Opacity.IMPLICIT, provisional=child.provisional)


DOMAIN_TYPE_AGGREGATES: Mapping[type[exp.AggFunc], AggregateRule[DomainTag]] = {
    exp.Sum: AggregateRule(core=_passthrough_core),
    exp.Avg: AggregateRule(core=_passthrough_core),
    exp.Min: AggregateRule(core=_passthrough_core),
    exp.Max: AggregateRule(core=_passthrough_core),
    exp.Count: AggregateRule(core=_count_core),
}


def companion_columns(tag: DomainTag) -> frozenset[ColumnRef]:
    """The per-row companion columns a tag's meaning rests on: every ``PerRow``
    unit in the dimension and every ``PerRow`` nominal binding. These are what an
    aggregate's coherence guard must prove constant per group; a ``Concrete``
    identity is constant everywhere, so it asks for nothing."""
    if isinstance(tag, _Conflict):
        return frozenset()
    out: set[ColumnRef] = set()
    if tag.dimension is not None:
        out.update(unit.column for unit, _ in tag.dimension.exponents if isinstance(unit, PerRow))
    out.update(binding.column for _, binding in tag.nominal if isinstance(binding, PerRow))
    return frozenset(out)


def _guarded_aggregates(
    fd: PropertyRef[FDSet, SourceRef],
) -> Mapping[type[exp.AggFunc], AggregateRule[DomainTag]]:
    """The aggregate catalog with the coherence obligation armed on the combining
    aggregates. ``sum`` and ``avg`` synthesize a new value out of many, so a
    varying tag corrupts the result and the guard clears it unless discharged.
    ``min``/``max`` select an existing value and ``count`` ignores values, so
    they keep their unguarded rules."""
    guard = CoherenceGuard(fd=fd, companions=companion_columns, entails=determines)
    return {
        **DOMAIN_TYPE_AGGREGATES,
        exp.Sum: AggregateRule(core=_passthrough_core, coherence=guard),
        exp.Avg: AggregateRule(core=_passthrough_core, coherence=guard),
    }


# --- the property ------------------------------------------------------------


def domain_type_grounding(
    facts: Mapping[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]],
    *,
    opaque: Collection[ColumnRef] = (),
) -> Callable[[ColumnRef], Annotation[DomainTag]]:
    """Fold the per-column domain-type facts into grounded annotations. The same fold
    every property uses: an opt-out grounds EXPLICIT top, a resolved bucket grounds its
    value CONCRETE, everything else the IMPLICIT-top default."""
    return grounding(facts, opaque, DOMAIN_TYPE_LATTICE)


def domain_type_property(
    ground: Callable[[ColumnRef], Annotation[DomainTag]],
    *,
    fd: PropertyRef[FDSet, SourceRef] | None = None,
) -> Property[DomainTag, ColumnRef]:
    """The column-scoped domain-type property over a caller-supplied grounding.

    The transfer catalogs (the Kennedy arithmetic, the additive agree-or-conflict
    rule, the tag-passthrough aggregates) are the reusable surface; the grounding is
    the only part that varies between a synthetic-fact test and the eventual contract
    bridge. No semiring: a confluence widens by the lattice ``join``, which is the
    correct "keep only what both arms agree on" for tags.

    Passing the functional-dependency property's ref arms the coherence guard on
    ``sum`` and ``avg``: an aggregate over a per-row companion tag keeps its tag
    only where the group key holds the companion constant (membership, a pin, or
    an FD entailment at the aggregation input) and clears to top otherwise. The
    edge is declared in ``depends_on``, so the registry evaluates dependencies
    first and the guard's read is always answered.
    """
    return column_property(
        name="domain_type",
        lattice=DOMAIN_TYPE_LATTICE,
        operators=DOMAIN_TYPE_OPERATORS,
        aggregates=DOMAIN_TYPE_AGGREGATES if fd is None else _guarded_aggregates(fd),
        ground=ground,
        depends_on=() if fd is None else (fd,),
    )
