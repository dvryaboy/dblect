"""Property[K] and the single-pass propagator.

A ``Property[K]`` bundles a ``Semiring[K]`` with three dispatch tables:

* ``operators``: per sqlglot ``Expr`` subclass, how the operator's input
  K-annotations combine into the output K-annotation.
* ``aggregates``: per sqlglot ``AggFunc`` subclass, the semimodule transfer
  (Amsterdamer, Deutch, Tannen 2011) from the aggregated input to the
  aggregate's output.
* ``source``: how a leaf ``ColumnRef`` (a dbt source or seed column) gets its
  initial K.

``propagate`` walks each output column's projection expression top-down. At
leaf ``exp.Column`` references it recursively resolves the upstream column
identified by metadata the builder attached. At internal nodes it dispatches
on the expression subclass. Unknown subclasses fold children via
``semiring.times`` and, lacking children, return ``Property.default()``;
detectors that consume the annotation stay silent rather than guess.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import reduce
from typing import Any, Generic, TypeVar, cast

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.graph import ColumnLineageGraph, ColumnRef
from dblect.lineage.semiring import Semiring

K = TypeVar("K")

OperatorTransfer = Callable[[Expr, tuple[K, ...]], K]
AggregateTransfer = Callable[[exp.AggFunc, K], K]
SourceRule = Callable[[ColumnRef], K]

# Key on ``Expr.meta`` where the builder records the ``ColumnRef`` an
# ``exp.Column`` resolves to. Centralised so builder and propagator stay in
# sync; tests pin the contract.
COLUMNREF_META_KEY = "dblect_columnref"


@dataclass(frozen=True, slots=True)
class Property(Generic[K]):
    """A propagated property: semiring, dispatch tables, and source rule.

    ``unknown_value`` is what the propagator returns when an expression has no
    transfer and no children to fold over (e.g., a literal like ``42``). For
    most properties this is ``semiring.zero``; lattice-shaped properties may
    prefer lattice-top to encode "we know nothing here". Left as ``None`` it
    falls back to ``semiring.zero`` at call time.

    The dispatch tables (``operators``, ``aggregates``) are required arguments
    even when empty, so each property declares its surface explicitly rather
    than relying on a default that hides what is or isn't covered.
    """

    name: str
    semiring: Semiring[K]
    source: SourceRule[K]
    operators: Mapping[type[Expr], OperatorTransfer[K]]
    aggregates: Mapping[type[exp.AggFunc], AggregateTransfer[K]]
    unknown_value: K | None = None

    def default(self) -> K:
        if self.unknown_value is not None:
            return self.unknown_value
        return self.semiring.zero


def propagate(graph: ColumnLineageGraph, prop: Property[K]) -> Mapping[ColumnRef, K]:
    """Compute ``prop``'s K-annotation for every column referenced by ``graph``.

    Walks each column's projection expression top-down, recursing into upstream
    column annotations at ``exp.Column`` leaves. Results are memoised: a column
    is annotated once per call regardless of how many downstream paths visit it.

    Cycles cannot occur in a manifest-derived lineage graph (the DAG is
    acyclic), but a defensive guard treats any in-progress recursion as the
    property's default value, so a malformed input degrades rather than recurses
    forever.
    """
    annotations: dict[ColumnRef, K] = {}
    in_progress: set[ColumnRef] = set()

    def annotate(col: ColumnRef) -> K:
        if col in annotations:
            return annotations[col]
        if col in in_progress:
            return prop.default()
        in_progress.add(col)
        try:
            expr = graph.expressions.get(col)
            value = prop.source(col) if expr is None else _walk(expr, prop, annotate)
            annotations[col] = value
            return value
        finally:
            in_progress.discard(col)

    for col in graph.expressions:
        annotate(col)
    for col in graph.edges:
        annotate(col)
    return annotations


def _walk(
    expr: Expr,
    prop: Property[K],
    annotate: Callable[[ColumnRef], K],
) -> K:
    """Recursively reduce ``expr`` to a K via ``prop``'s dispatch tables.

    Dispatch order:

    1. ``exp.Alias`` is a no-op, recurse into the wrapped expression.
    2. ``exp.Column`` resolves to the upstream annotation via the ``ColumnRef``
       the builder recorded in ``expr.meta``.
    3. ``exp.AggFunc`` subclass goes through ``prop.aggregates`` (with MRO
       lookup, so a transfer on ``AggFunc`` catches every subclass that has
       no more specific entry).
    4. Any other ``Expr`` subclass goes through ``prop.operators``.
    5. If no transfer is registered, fall back to folding ``Expr`` children
       via ``semiring.times``; with no expression children, return
       ``prop.default()``.
    """
    if isinstance(expr, exp.Alias):
        inner = expr.this
        if isinstance(inner, Expr):
            return _walk(inner, prop, annotate)
        return prop.default()
    if isinstance(expr, exp.Column):
        ref = _column_ref_meta(expr)
        if ref is None:
            return prop.default()
        return annotate(ref)
    if isinstance(expr, exp.AggFunc):
        agg_transfer = _lookup_subclass(prop.aggregates, type(expr))
        if agg_transfer is not None:
            child = expr.this if isinstance(expr.this, Expr) else None
            if child is None:
                return prop.default()
            child_k = _walk(child, prop, annotate)
            return agg_transfer(expr, child_k)
        # An aggregate without a registered transfer falls through to operator
        # dispatch (which itself defaults to folding children via times).
    op_transfer = _lookup_subclass(prop.operators, type(expr))
    children = _expression_children(expr)
    child_ks = tuple(_walk(c, prop, annotate) for c in children)
    if op_transfer is not None:
        return op_transfer(expr, child_ks)
    if not child_ks:
        return prop.default()
    return reduce(prop.semiring.times, child_ks)


def _expression_children(expr: Expr) -> tuple[Expr, ...]:
    """All ``Expr`` children of ``expr`` in document order.

    sqlglot stores expression arguments in ``expr.args``, a dict whose values
    are either a single ``Expr``, a list of ``Expr``s, or a non-expression
    value (string flag, etc.). This flattens the lot, dropping non-Expr values.
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
    """Find a transfer registered for ``cls`` or any of its base classes.

    sqlglot's ``Sum``/``Min``/``Max`` share an ``AggFunc`` ancestor; a property
    that registers a transfer on ``AggFunc`` itself can therefore catch all
    aggregates with one entry, while still permitting per-aggregate overrides.

    Typed as ``Mapping[type[Any], _T_TRANSFER]`` rather than narrowing to ``Expr``
    so the same helper serves both ``Property.operators`` (keyed on ``Expr``
    subclasses) and ``Property.aggregates`` (keyed on ``AggFunc`` subclasses)
    without running into Mapping-key invariance.
    """
    if cls in table:
        return table[cls]
    for base in cls.__mro__[1:]:
        if base in table:
            return table[base]
    return None


def _column_ref_meta(col: exp.Column) -> ColumnRef | None:
    """Read the ``ColumnRef`` the builder attached to this ``exp.Column``."""
    meta = col.meta.get(COLUMNREF_META_KEY)
    if isinstance(meta, ColumnRef):
        return meta
    return None


def attach_column_ref(col: exp.Column, ref: ColumnRef) -> None:
    """Builder-side hook: stamp ``col`` with its resolved ``ColumnRef``.

    The propagator reads back via ``Expr.meta``; this function exists so the
    meta-key string lives in one place.
    """
    col.meta[COLUMNREF_META_KEY] = ref
