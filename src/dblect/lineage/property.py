"""The single-pass propagator that walks a column lineage graph for one property.

A :class:`~dblect.lineage.facts.Property` says how a node is grounded from
declarations (its ``ground`` function), how values combine at operators and
aggregates (its transfer catalogs), and how to order values for resolution (its
lattice). This module walks the graph once per property, carrying an
:class:`~dblect.lineage.facts.Annotation` at every node:

* At a node with no derivation (a source or seed) it flows the node's *grounded*
  annotation from ``ground``.
* At a node grounded opaque (``EXPLICIT``) it short-circuits, flowing top
  silently because the modeller took responsibility for it.
* At a derived node it reduces the projection expression to an *inferred*
  annotation, then reconciles it against the grounded value into the **flow**
  value: a more precise inferred value tightens, an opaque inference keeps the
  grounded value, and a conflict keeps it but taints it provisional.

Confluences (``UNION ALL``) fold with the property's ``semiring.plus`` when it
carries one, otherwise the lattice join; multi-input scalars fold with
``semiring.times`` or, lacking a semiring, the lattice join. The calculus and its
obligations are in ``docs/design/propagation-soundness.md``.

``run`` drives a :class:`~dblect.lineage.facts.registry.PropertyRegistry`,
evaluating properties in dependency order and accumulating their annotations into
an :class:`~dblect.lineage.facts.registry.AnnotationStore` that each later
property's :class:`~dblect.lineage.facts.DepContext` reads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from functools import reduce
from typing import Any, ClassVar, TypeVar, cast

import sqlglot.expressions as exp
from sqlglot import Expr
from sqlglot.expressions.core import Expression

from dblect.lineage.facts.lattice import Lattice, consistent
from dblect.lineage.facts.model import Annotation, Opacity, ScopeKind
from dblect.lineage.facts.property import (
    AggregateRule,
    DepContext,
    OperatorTransfer,
    Property,
    Reducer,
)
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, LineageView, SourceRef

K = TypeVar("K")
S = TypeVar("S", ColumnRef, SourceRef)

# Key on ``Expr.meta`` where the builder records the single ``ColumnRef`` an
# ``exp.Column`` resolves to. Centralised so builder and propagator stay in sync.
COLUMNREF_META_KEY = "dblect_columnref"


class UnionConfluence(Expression):
    """Synthetic confluence node for a ``UNION ALL`` combined output column.

    Carries the per-arm ``ColumnRef``s on the instance so the propagator can fold
    them directly. Distinct from ``exp.Union`` (and not an ``exp.Column``), so
    qualifier passes and column resolution can never misread it as a real
    reference.
    """

    arg_types: ClassVar[dict[str, bool]] = {}

    def __init__(self, arm_refs: tuple[ColumnRef, ...] = ()) -> None:
        super().__init__()
        self.arm_refs: tuple[ColumnRef, ...] = arm_refs


class _NullDepContext:
    """The dependency view a property with no ``depends_on`` edges sees: every read
    is silent, which a transfer reads as the dependency's lattice top."""

    def annotation(self, ref: object, scope: object) -> None:
        return None


_NULL_DEP_CONTEXT: DepContext = cast("DepContext", _NullDepContext())


def propagate(
    graph: LineageView[S],
    prop: Property[K, S],
    *,
    dep_context: DepContext = _NULL_DEP_CONTEXT,
) -> Mapping[S, Annotation[K]]:
    """Compute ``prop``'s flow annotation for every subject in ``graph``.

    This is the one propagation engine: a memoised grounded fixpoint over the
    lineage DAG. For each subject it grounds a declared annotation, short-circuits
    on a declared opt-out, reduces the subject's derivation to an inferred
    annotation, and reconciles the two. The scope-specific part is the *reducer*:
    how a derivation reduces, and how recursion into referenced subjects is
    triggered. A property carries its own reducer (relation scope) or falls back
    to the generic column reducer. Everything else here is shared by column- and
    relation-scoped properties alike.

    Memoised per subject, so each is annotated once regardless of how many
    downstream paths touch it. A defensive cycle guard returns the property's
    no-information default if recursion ever revisits a subject mid-walk, so a
    malformed input degrades instead of looping forever (a manifest-derived graph
    is acyclic).
    """
    reduce = _reducer_for(prop)
    lat = prop.lattice
    check = consistent(lat)
    # The "no information" value a node grounds to when nothing derives or
    # declares it: a counting/accumulating property's additive identity
    # (semiring.zero), otherwise the lattice top. Both read as "we don't know".
    default_value = prop.semiring.zero if prop.semiring is not None else lat.top
    default_ann = Annotation(default_value, Opacity.IMPLICIT)
    annotations: dict[S, Annotation[K]] = {}
    in_progress: set[S] = set()

    def annotate(subject: S) -> Annotation[K]:
        if subject in annotations:
            return annotations[subject]
        if subject in in_progress:
            return default_ann
        in_progress.add(subject)
        try:
            grounded = prop.ground(subject)
            if grounded.opacity is Opacity.EXPLICIT:
                result = grounded
            else:
                deriv = graph.derivation(subject)
                if deriv is None:
                    result = grounded  # a leaf anchors on its grounded value
                else:
                    inferred = reduce(deriv, prop, annotate, dep_context, default_ann)
                    result = _reconcile(lat, check, grounded, inferred, prop.reconcile_by_meet)
            annotations[subject] = result
            return result
        finally:
            in_progress.discard(subject)

    for subject in graph.subjects():
        annotate(subject)
    return annotations


def _reducer_for(prop: Property[Any, Any]) -> Reducer:
    """The reducer this property propagates with, resolved at the single dispatch
    point so a missing one raises here rather than mid-walk.

    A property that carries its own ``reducer`` uses it. Otherwise column scope
    falls back to the generic ``_column_reduce``; relation scope has no generic
    reducer, so a relation property that supplies none cannot propagate."""
    if prop.reducer is not None:
        return prop.reducer
    if prop.scope_kind is ScopeKind.COLUMN:
        return _column_reduce
    raise NotImplementedError(
        f"relation-scoped property {prop.name!r} carries no reducer; a relation "
        "property must supply its relation-algebra walk, since relation reduction "
        "has no generic default"
    )


def run(graph: ColumnLineageGraph, registry: PropertyRegistry) -> AnnotationStore:
    """Walk ``graph`` for every property in dependency order, accumulating each
    node's flow annotation into a shared store that later properties read."""
    store = AnnotationStore()
    for prop in registry.evaluation_order():
        ctx = registry.dep_context(store)
        for scope, annotation in propagate(graph, prop, dep_context=ctx).items():
            store.record(prop.name, scope, annotation)
    return store


def _reconcile(
    lat: Lattice[K],
    check: Callable[[K, K], bool],
    grounded: Annotation[K],
    inferred: Annotation[K],
    reconcile_by_meet: bool,
) -> Annotation[K]:
    """Combine a derived node's grounded and inferred annotations into its flow value.

    Nothing grounded: the SQL stands. An opaque inference keeps the grounded value.

    When ``reconcile_by_meet`` (a same-polarity property like uniqueness), declared
    and inferred are both lower bounds, so the flow value is their meet and there is
    no conflict: meet subsumes the tighten case (a refining inferred meets to itself)
    and unions otherwise.

    Otherwise (the default, value-domain like nullability): a more precise
    (consistent) inference tightens to the inferred value; a conflict keeps the
    grounded value as the contract but taints it provisional, so one upstream
    regression does not blank analysis of every consumer.
    """
    if grounded.opacity is Opacity.IMPLICIT:
        return inferred
    if inferred.value == lat.top:
        return grounded
    if reconcile_by_meet:
        provisional = grounded.provisional or inferred.provisional
        return Annotation(lat.meet(grounded.value, inferred.value), grounded.opacity, provisional)
    if check(grounded.value, inferred.value):
        return inferred
    return Annotation(grounded.value, grounded.opacity, provisional=True)


def _column_reduce(
    expr: Expr,
    prop: Property[K, Any],
    annotate: Callable[[ColumnRef], Annotation[K]],
    dep_context: DepContext,
    default_ann: Annotation[K],
) -> Annotation[K]:
    """The column-scoped reducer: reduce a projection ``expr`` to one annotation.

    Dispatch, most-specific first: an ``Alias`` looks through; a ``Column``
    recurses into its stamped upstream ref; a ``UnionConfluence`` folds its arms
    with the confluence combine; an aggregate with a registered rule applies its
    ``core``; any other expression consults ``operators`` and otherwise folds its
    children with the scalar combine. An expression with no children grounds to
    the lattice top.
    """
    lat = prop.lattice

    if isinstance(expr, exp.Alias):
        inner = expr.this
        return (
            _column_reduce(inner, prop, annotate, dep_context, default_ann)
            if isinstance(inner, Expr)
            else default_ann
        )

    if isinstance(expr, exp.Column):
        ref = _column_ref_meta(expr)
        return annotate(ref) if ref is not None else default_ann

    if isinstance(expr, UnionConfluence):
        if not expr.arm_refs:
            return default_ann
        combine = prop.semiring.plus if prop.semiring is not None else lat.join
        return _fold(lat, combine, (annotate(r) for r in expr.arm_refs))

    if isinstance(expr, exp.AggFunc):
        rule = _lookup_subclass(prop.aggregates, type(expr))
        if rule is not None:
            child = expr.this if isinstance(expr.this, Expr) else None
            if child is None:
                return default_ann
            child_ann = _column_reduce(child, prop, annotate, dep_context, default_ann)
            return _apply_aggregate(rule, expr, child_ann)
        # An aggregate with no registered rule falls through to operator dispatch.

    op = _lookup_subclass(prop.operators, type(expr))
    child_anns = tuple(
        _column_reduce(c, prop, annotate, dep_context, default_ann)
        for c in _expression_children(expr)
    )
    if op is not None:
        return op(expr, child_anns, dep_context)
    if not child_anns:
        return default_ann
    combine = prop.semiring.times if prop.semiring is not None else lat.join
    return _fold(lat, combine, child_anns)


def _apply_aggregate(
    rule: AggregateRule[K], expr: exp.AggFunc, child: Annotation[K]
) -> Annotation[K]:
    """Apply an aggregate rule's pure ``core``.

    The optional coherence guard (an FD read that clears to top on failure) reads a
    dependency property and lands with the first aggregate that needs it; the
    shipping properties carry no guard, so the core is the whole rule here.
    """
    return rule.core(expr, child)


def _fold(
    lat: Lattice[K], combine: Callable[[K, K], K], anns: Iterable[Annotation[K]]
) -> Annotation[K]:
    """Combine annotation values with ``combine`` and derive the result's opacity.

    A non-top result is ``CONCRETE``. A top result is ``EXPLICIT`` if any input was
    a declared opt-out, otherwise ``IMPLICIT``. The provisional taint is the OR of
    the inputs'.
    """
    items = list(anns)
    value = reduce(combine, (a.value for a in items))
    provisional = any(a.provisional for a in items)
    if value != lat.top:
        return Annotation(value, Opacity.CONCRETE, provisional=provisional)
    explicit = any(a.opacity is Opacity.EXPLICIT for a in items)
    opacity = Opacity.EXPLICIT if explicit else Opacity.IMPLICIT
    return Annotation(value, opacity, provisional=provisional)


def _expression_children(expr: Expr) -> tuple[Expr, ...]:
    """All ``Expr`` children of ``expr`` in document order.

    sqlglot stores expression arguments in ``expr.args``, a dict whose values are a
    single ``Expr``, a list of ``Expr``s, or a non-expression value (string flag,
    etc.). This flattens the lot, dropping non-Expr values.
    """
    out: list[Expr] = []
    for value in expr.args.values():
        if isinstance(value, Expr):
            out.append(value)
        elif isinstance(value, list):
            out.extend(item for item in cast("list[object]", value) if isinstance(item, Expr))
    return tuple(out)


_T_TRANSFER = TypeVar("_T_TRANSFER")


def _lookup_subclass(table: Mapping[type[Any], _T_TRANSFER], cls: type) -> _T_TRANSFER | None:
    """Find a rule registered for ``cls`` or any of its base classes.

    sqlglot's ``Sum``/``Min``/``Max`` share an ``AggFunc`` ancestor; a property that
    registers a rule on ``AggFunc`` itself catches all aggregates with one entry,
    while still permitting per-aggregate overrides. Typed on ``type[Any]`` so the
    same helper serves ``operators`` and ``aggregates`` without Mapping-key
    invariance trouble.
    """
    if cls in table:
        return table[cls]
    for base in cls.__mro__[1:]:
        if base in table:
            return table[base]
    return None


def _column_ref_meta(col: exp.Column) -> ColumnRef | None:
    """Read the ``ColumnRef`` the builder stamped on ``col``; ``None`` if unstamped."""
    meta = col.meta.get(COLUMNREF_META_KEY)
    return meta if isinstance(meta, ColumnRef) else None


def attach_column_ref(col: exp.Column, ref: ColumnRef) -> None:
    """Stamp ``col`` with the single ``ColumnRef`` it resolves to."""
    col.meta[COLUMNREF_META_KEY] = ref


# The operator-transfer alias re-exported for callers that build properties next
# to the propagator; the canonical definition lives in dblect.lineage.facts.
__all__ = [
    "COLUMNREF_META_KEY",
    "OperatorTransfer",
    "UnionConfluence",
    "attach_column_ref",
    "propagate",
    "run",
]
