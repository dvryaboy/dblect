"""Turning declarations into grounded annotations: collection, grounding, and
the typed/untyped seam combine.

``collect`` runs the discoverers and buckets their facts by scope; ``grounding``
folds each bucket through ``resolve`` into the per-node grounded annotation
(EXPLICIT for an opt-out, CONCRETE for a resolved value, IMPLICIT otherwise);
``combine`` is the binary seam rule that decides whether a cleared refinement
speaks. The errors are a small sealed set so a caller can react to each.
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
    """Raised by ``combine`` when two committed, incompatible operands meet at a
    scalar expression. It becomes a finding at the combine site."""

    def __init__(self, a: Annotation[Any], b: Annotation[Any]) -> None:
        self.a = a
        self.b = b
        super().__init__(f"incompatible operands at a seam: {a.value!r} and {b.value!r}")


class DiscovererError(Exception):
    """The one exception ``collect`` treats as expected: a discoverer that hits a
    manifest shape it cannot read raises this, drops all of its own facts, and
    leaves every other discoverer's facts untouched. Any other exception is a
    substrate bug and propagates."""


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


def grounding(
    facts: Mapping[S, tuple[Fact[K, S], ...]],
    opaque: Collection[S],
    lat: Lattice[K],
) -> Callable[[S], Annotation[K]]:
    """Fold each scope's bucket into its grounded annotation and return the lookup.

    An opaque scope grounds ``Annotation(top, EXPLICIT)`` regardless of any facts
    present (an opt-out is consulted before facts). A scope whose bucket resolves
    grounds ``Annotation(value, CONCRETE)``. Every other scope grounds
    ``Annotation(top, IMPLICIT)``, the "nothing declared" default.

    A bucket that resolves to ``bottom`` is a contradiction and raises
    ``FactConflictError`` here, at build time, rather than swallowing it; recovery
    is a propagator concern that lands with the findings layer.
    """
    opaque_set = set(opaque)
    grounded: dict[S, Annotation[K]] = {}
    for scope in opaque_set:
        grounded[scope] = Annotation(lat.top, Opacity.EXPLICIT)
    for scope, bucket in facts.items():
        if scope in opaque_set:
            continue  # the opt-out already won
        value, is_contradiction = resolve(lat, bucket)
        if is_contradiction:
            raise FactConflictError(scope, tuple(bucket))
        grounded[scope] = Annotation(value, Opacity.CONCRETE)

    implicit_top: Annotation[K] = Annotation(lat.top, Opacity.IMPLICIT)

    def ground(scope: S) -> Annotation[K]:
        return grounded.get(scope, implicit_top)

    return ground


def combine(lat: Lattice[K], a: Annotation[K], b: Annotation[K]) -> Annotation[K]:
    """The binary seam rule at a scalar expression.

    Meet the two values; a ``bottom`` meet is two committed, incompatible operands
    and raises ``SeamContradictionError``. Agreeing operands preserve their value. When
    one operand is top and the other committed, the result clears to top and
    inherits *that operand's* opacity, so an un-annotated (IMPLICIT) clear speaks
    at the seam while a declared (EXPLICIT) opt-out flows silently.
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
