"""``Property[K]`` and the single-pass propagator that walks lineage graphs.

A property has three pieces:

* ``source``: how a leaf column (a dbt source or seed) gets its starting
  value. For where-provenance it's "the singleton set containing this
  column"; for nullability it might be "True if the column is declared
  nullable in the manifest."
* ``operators``: per sqlglot expression type, how the input values combine
  into the output value. Empty by default: the walker then folds inputs via
  ``semiring.times`` (which is union for where-provenance, ``and`` for the
  Boolean semiring, etc.).
* ``aggregates``: per sqlglot aggregate function, the rule for going from
  the aggregated input's value to the aggregate's output. This is the
  semimodule layer on top of the semiring (Amsterdamer, Deutch, Tannen
  "Provenance for Aggregate Queries", PODS 2011); for many properties the
  default ``times``-fold is correct, others (uniqueness through GROUP BY,
  fanout through COUNT) install a specific rule here.

``propagate(graph, prop)`` walks each column's projection expression
top-down. At an ``exp.Column`` leaf it recurses into the single upstream
``ColumnRef`` the builder stamped onto the node. At an internal expression
it dispatches on the type. ``exp.Union`` is structurally a confluence
point and always folds its arms via ``semiring.plus`` (the same role
``UNION ALL`` plays in K-relations); other expressions consult
``Property.operators`` / ``Property.aggregates`` and fall through to a
``semiring.times`` fold of children. An expression with no children at all
returns ``Property.default()`` so detectors stay silent rather than guess.
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

# Key on ``Expr.meta`` where the builder records the single ``ColumnRef``
# an ``exp.Column`` resolves to. Centralised so builder and propagator stay
# in sync; tests pin the contract.
COLUMNREF_META_KEY = "dblect_columnref"


@dataclass(frozen=True, slots=True)
class Property(Generic[K]):
    """A propagated property: how to start, how to combine, and what to do
    when we don't know.

    ``unknown_value`` is what the propagator returns when an expression has
    no registered rule and no children to fold over (e.g., a literal like
    ``42``). For most properties this is ``semiring.zero``; lattice-shaped
    properties may want lattice-top to mean "we know nothing here." Left as
    ``None`` it defaults to ``semiring.zero``.

    ``operators`` and ``aggregates`` are required arguments even when empty,
    so each property declares its surface explicitly: a reader can tell at
    a glance which expression types this property treats specially.
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
    """Compute ``prop``'s value for every column in ``graph``.

    Walks each output column's projection expression top-down and recurses
    into upstream columns at ``exp.Column`` leaves. Memoised per column, so
    each column is annotated once regardless of how many downstream paths
    touch it.

    Cycles can't occur in a manifest-derived lineage graph (the DAG is
    acyclic), but a defensive guard returns the property's default if
    recursion ever revisits a column mid-walk, so a malformed input degrades
    instead of looping forever.
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
    """Reduce ``expr`` to a single value by walking it and combining children.

    Dispatch order, from most-specific to least:

    1. ``Alias`` is a wrapper; look through it to the wrapped expression.
    2. ``Column`` recurses into the single upstream ``ColumnRef`` the
       builder stamped onto it. An unstamped Column returns
       ``prop.default()`` (the builder couldn't resolve it).
    3. ``Union`` is the K-relations confluence point. Its arm children
       always fold via ``semiring.plus``, regardless of any operator rule
       the property might have registered: the algebra requires
       confluences to fold with ``plus``, and pretending otherwise would
       silently violate property contracts. With no arm children at all
       (a malformed Union), return ``prop.default()``.
    4. Aggregates (``AggFunc`` subclasses) check ``prop.aggregates`` with
       MRO lookup, so a rule registered on ``AggFunc`` itself catches every
       aggregate subclass that has no more specific entry.
    5. Any other expression type checks ``prop.operators`` (also MRO).
    6. With no registered rule, fold the expression's children via
       ``semiring.times``. With no expression children at all, return
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
    if isinstance(expr, exp.Union):
        arm_ks = tuple(_walk(c, prop, annotate) for c in _expression_children(expr))
        if not arm_ks:
            return prop.default()
        return reduce(prop.semiring.plus, arm_ks)
    if isinstance(expr, exp.AggFunc):
        agg_transfer = _lookup_subclass(prop.aggregates, type(expr))
        if agg_transfer is not None:
            child = expr.this if isinstance(expr.this, Expr) else None
            if child is None:
                return prop.default()
            child_k = _walk(child, prop, annotate)
            return agg_transfer(expr, child_k)
        # An aggregate without a registered rule falls through to operator
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
    """Find a rule registered for ``cls`` or any of its base classes.

    sqlglot's ``Sum``/``Min``/``Max`` share an ``AggFunc`` ancestor; a property
    that registers a rule on ``AggFunc`` itself can therefore catch all
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
    """Read the single ``ColumnRef`` the builder attached to this ``exp.Column``.

    Returns ``None`` for an unstamped Column (either the builder couldn't
    resolve the reference or it never visited the node). The propagator
    treats that as "unknown" and falls back to ``Property.default()``.
    """
    meta = col.meta.get(COLUMNREF_META_KEY)
    if isinstance(meta, ColumnRef):
        return cast("ColumnRef", meta)
    return None


def attach_column_ref(col: exp.Column, ref: ColumnRef) -> None:
    """Builder-side hook: stamp ``col`` with the single ``ColumnRef`` it resolves to.

    Each ``exp.Column`` in a projection expression points at exactly one
    immediate upstream column in the lineage graph. That upstream may
    itself be a CTE intermediate, a UNION arm, an upstream-model column,
    or a leaf source; the propagator recurses through the chain.
    Multi-leaf fan-out is handled by materialising CTE / union nodes in
    the graph rather than by attaching multiple refs here.
    """
    col.meta[COLUMNREF_META_KEY] = ref
