"""Resolving what a user declared into what dblect actually knows about a
column or relation.

``collect`` runs the discoverers and buckets their facts by scope; ``grounding``
folds each bucket into one value per scope: CONCRETE for a resolved value,
EXPLICIT for a declared opt-out, IMPLICIT when nothing was declared at all;
``combine`` is the rule for two such values meeting at an expression: it
decides whether an unresolved side warns or stays quiet. The errors below are
a small, closed set so a caller can react to each.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from dblect.lineage.facts.lattice import Lattice, resolve
from dblect.lineage.facts.model import Annotation, Fact, Opacity
from dblect.lineage.graph import ColumnRef, SourceRef

if TYPE_CHECKING:
    from dblect.lineage.facts.property import FactDiscoverer
    from dblect.manifest import Manifest

K = TypeVar("K")
S = TypeVar("S", ColumnRef, SourceRef)
_S_co = TypeVar("_S_co", ColumnRef, SourceRef, covariant=True)


@runtime_checkable
class OpaqueReader(Protocol[_S_co]):
    """Reads the opaque opt-out channels (a ``meta.dblect.opaque`` key, an
    ``OpaqueEffect`` on a contract, an inline ``dblect: opaque`` marker) and
    returns the scopes opted out of refinement. Its result feeds ``grounding`` as
    the ``opaque`` set, consulted before facts; a discoverer never emits a
    top-valued fact, so an opt-out is synthesized here rather than stored."""

    def opaque_scopes(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[_S_co]: ...


class FactConflictError(Exception):
    """Raised when a scope's facts meet to the lattice bottom: the declarations
    are mutually unsatisfiable. Carries the scope and the conflicting facts so the
    audit can report them."""

    def __init__(self, scope: object, facts: tuple[Fact[Any, Any], ...]) -> None:
        self.scope = scope
        self.facts = facts
        super().__init__(f"contradictory facts at {scope!r}: {[f.value for f in facts]}")


class SeamContradictionError(Exception):
    """Raised by ``combine`` when two concrete values meeting at the same
    expression disagree, so there is no value both sides could be right about.
    Surfaces as a finding at that expression."""

    def __init__(self, a: Annotation[Any], b: Annotation[Any]) -> None:
        self.a = a
        self.b = b
        super().__init__(f"incompatible operands at a seam: {a.value!r} and {b.value!r}")


class DiscovererError(Exception):
    """The one exception ``collect`` treats as expected: a discoverer that hits a
    manifest shape it cannot read raises this, drops all of its own facts, and
    leaves every other discoverer's facts untouched. Any other exception is a
    bug in dblect itself and propagates."""


def collect(
    manifest: Manifest,
    discoverers: tuple[FactDiscoverer[K, S], ...],
    *,
    name_to_source: Mapping[str, SourceRef],
) -> Mapping[S, tuple[Fact[K, S], ...]]:
    """Run each discoverer and bucket its facts by scope.

    A discoverer that raises a ``DiscovererError`` contributes nothing and the
    others are unaffected; any other exception propagates, failing the build
    loudly rather than silently dropping facts.
    """
    buckets: dict[S, list[Fact[K, S]]] = {}
    for discoverer in discoverers:
        try:
            found = discoverer.discover(manifest, name_to_source=name_to_source)
        except DiscovererError:
            continue
        for fact in found:
            buckets.setdefault(fact.scope, []).append(fact)
    return {scope: tuple(facts) for scope, facts in buckets.items()}


def _ground(
    facts: Mapping[S, tuple[Fact[K, S], ...]],
    opaque: Collection[S],
    lat: Lattice[K],
) -> dict[S, Annotation[K]]:
    """Build the scope-to-annotation map, with no default filled in for a scope
    that has nothing recorded. ``grounding`` wraps this in the "nothing
    declared" default, and ``grounded_scopes`` reads it back to report which
    scopes actually came from a fact; both stay in sync because they share
    this one fold.

    A scope opted out of refinement always maps to top, tagged EXPLICIT, no
    matter what facts exist for it. Otherwise, a scope's facts that hold
    unconditionally (``condition is None``) resolve to one value, tagged
    CONCRETE. A fact scoped to a row filter is skipped here rather than
    applied everywhere, since that would claim more than it proved; if only
    such facts exist for a scope, the scope is left out of the map, exactly as
    if nothing were declared, though the facts themselves are kept for later
    filter-matching to use.

    Facts that cannot be reconciled to one value raise ``FactConflictError``
    here, at build time, instead of silently picking an answer.
    """
    opaque_set = set(opaque)
    grounded: dict[S, Annotation[K]] = {}
    for scope in opaque_set:
        grounded[scope] = Annotation(lat.top, Opacity.EXPLICIT)
    for scope, bucket in facts.items():
        if scope in opaque_set:
            continue  # the opt-out already won
        unconditional = tuple(f for f in bucket if f.condition is None)
        if not unconditional:
            continue  # only conditional facts here: nothing grounds unconditionally
        value, is_contradiction = resolve(lat, unconditional)
        if is_contradiction:
            raise FactConflictError(scope, unconditional)
        grounded[scope] = Annotation(value, Opacity.CONCRETE)
    return grounded


def grounding(
    facts: Mapping[S, tuple[Fact[K, S], ...]],
    opaque: Collection[S],
    lat: Lattice[K],
) -> Callable[[S], Annotation[K]]:
    """Fold each scope's bucket into its value and return the lookup.

    A scope absent from the fold grounds ``Annotation(top, IMPLICIT)``, the
    "nothing declared" default. See ``_ground`` for the grounding rule itself.
    """
    grounded = _ground(facts, opaque, lat)
    implicit_top: Annotation[K] = Annotation(lat.top, Opacity.IMPLICIT)

    def ground(scope: S) -> Annotation[K]:
        return grounded.get(scope, implicit_top)

    return ground


def grounded_scopes(
    facts: Mapping[S, tuple[Fact[K, S], ...]],
    opaque: Collection[S],
    lat: Lattice[K],
) -> set[S]:
    """The scopes a fact actually grounded (``CONCRETE``), which coverage reports
    as the grounded share. Reads the same fold ``grounding`` does, so the two can
    never disagree on what grounds. An opaque opt-out grounds ``EXPLICIT`` top
    rather than a value, so it is not a fact grounding and is excluded here."""
    return {
        scope
        for scope, ann in _ground(facts, opaque, lat).items()
        if ann.opacity is Opacity.CONCRETE
    }


def combine(lat: Lattice[K], a: Annotation[K], b: Annotation[K]) -> Annotation[K]:
    """The rule for two annotations meeting at one scalar expression, for
    example the two sides of a binary operator.

    Two concrete values that disagree raise ``SeamContradictionError``. Two
    that agree keep their shared value. When one side is concrete and the
    other has no information, the result loses the concrete side's
    information too, since the uninformed side could still turn out to break
    it; the opacity of *that* side then decides whether the loss warns or
    stays quiet: a declared opt-out (EXPLICIT) stays quiet, a genuine gap
    (IMPLICIT) warns.
    """
    provisional = a.provisional or b.provisional
    m = lat.meet(a.value, b.value)
    if m == lat.bottom:
        raise SeamContradictionError(a, b)
    if a.value == b.value == m:
        # Operands agree. When they agree on top, keep the stronger opacity claim
        # so a declared opt-out is not silently downgraded to incidental.
        if m == lat.top:
            opacity = Opacity.EXPLICIT if Opacity.EXPLICIT in (a.opacity, b.opacity) else a.opacity
            return Annotation(m, opacity=opacity, provisional=provisional)
        return Annotation(m, provisional=provisional)
    cleared = a if a.value == lat.top else b
    return Annotation(lat.top, opacity=cleared.opacity, provisional=provisional)
