"""Array-non-emptiness: a per-column property over array-typed columns.

The lattice is the tri-state ``{NON_EMPTY, UNKNOWN}`` (with an unreachable
``CONTRADICTION`` bottom that only makes it bounded). ``NON_EMPTY`` refines
``UNKNOWN``, the "no information" top; ``meet`` keeps the stronger guarantee. The
firewall posture is the whole point: anything the walk cannot prove non-empty
stays ``UNKNOWN``, never over-claiming, so a consumer that clears a hazard on
``NON_EMPTY`` only ever clears what is provably safe.

The value is driven entirely by intrinsic facts the transfers read off the SQL,
not by anything a manifest declares, so the property needs no discoverer and
every column grounds to the implicit top:

* an intrinsic constructor whose non-emptiness the SQL vocabulary reads off the node
  alone is ``NON_EMPTY``: an ``ARRAY[...]`` / ``ARRAY(...)`` with one or more elements, or
  a ``GENERATE_ARRAY`` / ``GENERATE_SERIES`` / ``GENERATE_DATE_ARRAY`` /
  ``GENERATE_TIMESTAMP_ARRAY`` whose literal bounds make the range non-empty;
* an ``ARRAY_AGG(expr)`` under a ``GROUP BY`` is ``NON_EMPTY`` per group, since a
  group has at least one row and so at least one element (even a NULL element
  counts toward non-emptiness). Without a ``GROUP BY`` the fold is over the whole
  relation, where ``ARRAY_AGG`` of zero rows returns NULL, so it stays ``UNKNOWN``;
* ``ARRAY_AGG(expr IGNORE NULLS)`` is ``NON_EMPTY`` only when ``expr`` is provably
  non-null (a ``STRUCT(...)`` constructor is never null). Otherwise an all-NULL
  group collapses to ``[]`` and the value stays ``UNKNOWN``;
* a base-relation array column grounds ``UNKNOWN`` (its emptiness is an ingestion
  fact the static analyser cannot see), which is just the default top.

Transfer is the ordinary column walk: a passthrough or rename carries the value,
and a ``UNION ALL`` is ``NON_EMPTY`` only when every arm is (the lattice join over
``UNKNOWN``-as-top gives exactly that, so no semiring is needed). The consumer is
the inner-flatten row-drop detector, which treats an ``UNNEST`` of a provably
``NON_EMPTY`` argument as row-preserving.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Opacity
from dblect.lineage.facts.property import (
    AggregateRule,
    DepContext,
    Property,
    column_property,
)
from dblect.lineage.graph import ColumnRef, aggregation_site_meta
from dblect.sql.vocab import array_literal_nonempty, generator_provably_nonempty


class ArrayNonEmpty(StrEnum):
    CONTRADICTION = "contradiction"  # formal lattice bottom; unreachable in propagation
    NON_EMPTY = "non_empty"
    UNKNOWN = "unknown"


# Precision rank: smaller is more precise. CONTRADICTION < NON_EMPTY < UNKNOWN.
_RANK: dict[ArrayNonEmpty, int] = {
    ArrayNonEmpty.CONTRADICTION: 0,
    ArrayNonEmpty.NON_EMPTY: 1,
    ArrayNonEmpty.UNKNOWN: 2,
}


def _meet(a: ArrayNonEmpty, b: ArrayNonEmpty) -> ArrayNonEmpty:
    return a if _RANK[a] <= _RANK[b] else b


def _join(a: ArrayNonEmpty, b: ArrayNonEmpty) -> ArrayNonEmpty:
    return a if _RANK[a] >= _RANK[b] else b


ARRAY_NONEMPTY_LATTICE: Lattice[ArrayNonEmpty] = Lattice(
    meet=_meet,
    join=_join,
    top=ArrayNonEmpty.UNKNOWN,
    bottom=ArrayNonEmpty.CONTRADICTION,
)

_NON_EMPTY = Annotation(ArrayNonEmpty.NON_EMPTY)
_UNKNOWN = Annotation(ArrayNonEmpty.UNKNOWN, Opacity.IMPLICIT)


def _is_grouped(agg: exp.AggFunc) -> bool:
    """Whether the aggregate folds per group rather than over the whole relation.

    Read off the :class:`AggregationSite` the builder stamps on every non-windowed
    aggregate: an empty ``group_refs`` is the whole-relation fold (no GROUP BY, or
    the ``GROUP BY ()`` grand-total grouping set), a non-empty set is a resolved
    GROUP BY, and ``None`` is a GROUP BY whose keys the builder could not resolve to
    plain columns (positional or computed). A non-empty set and the ``None`` case
    both name a real GROUP BY that guarantees at least one row per group, so both
    count as grouped; only the empty set, or an absent site (a windowed aggregate,
    an unmodelled scope), is treated as whole-relation."""
    site = aggregation_site_meta(agg)
    if site is None:
        return False
    return site.group_refs != frozenset()


def _aggregated_expr(agg: exp.AggFunc) -> Expr:
    """The expression an aggregate folds, looking through an ``ORDER BY``.

    ``ARRAY_AGG(STRUCT(...) ORDER BY ts)`` parses with ``agg.this`` an ``exp.Order``
    wrapping the real argument, so reading ``agg.this`` directly would miss the
    ``STRUCT`` underneath. The order clause changes element order, never element
    presence, so it is transparent to non-emptiness."""
    return agg.this.this if isinstance(agg.this, exp.Order) else agg.this


def _array_agg_value(agg: exp.AggFunc, *, ignore_nulls: bool) -> Annotation[ArrayNonEmpty]:
    """The non-emptiness an ``ARRAY_AGG`` grounds, given whether it drops nulls.

    A grouped aggregate has at least one row per group. With nulls kept, that row
    contributes at least one element (a NULL element still counts), so the array is
    ``NON_EMPTY``. With ``IGNORE NULLS`` an all-NULL group collapses to ``[]``, so
    it is ``NON_EMPTY`` only when the aggregated expression is provably non-null;
    a ``STRUCT(...)`` constructor is the one such form we recognise (an ``ORDER BY``
    on the argument is transparent). A whole-relation fold returns NULL over zero
    rows, so it stays ``UNKNOWN``."""
    if not _is_grouped(agg):
        return _UNKNOWN
    if not ignore_nulls:
        return _NON_EMPTY
    return _NON_EMPTY if isinstance(_aggregated_expr(agg), exp.Struct) else _UNKNOWN


def _intrinsic_constructor_rule(
    predicate: Callable[[Expr], bool],
) -> Callable[[Expr, tuple[Annotation[ArrayNonEmpty], ...], DepContext], Annotation[ArrayNonEmpty]]:
    """A rule for a constructor whose non-emptiness the SQL vocabulary decides from the node
    alone, with no lineage: a literal ``ARRAY[...]``, a bounded ``GENERATE_ARRAY``. ``predicate``
    is the vocab check; a positive proof grounds ``NON_EMPTY``, anything else the ``UNKNOWN``
    top. The inner-flatten detector consults the same vocab predicates on inline arguments, so
    the local and cross-model paths recognise each constructor the same way."""

    def rule(
        expr: Expr, _kids: tuple[Annotation[ArrayNonEmpty], ...], _ctx: DepContext
    ) -> Annotation[ArrayNonEmpty]:
        return _NON_EMPTY if predicate(expr) else _UNKNOWN

    return rule


def _ignore_nulls_rule(
    expr: Expr, kids: tuple[Annotation[ArrayNonEmpty], ...], _ctx: DepContext
) -> Annotation[ArrayNonEmpty]:
    """``ARRAY_AGG(... IGNORE NULLS)`` parses as an ``IgnoreNulls`` wrapper around the
    aggregate. Recompute the aggregate's value knowing nulls are dropped; the inner
    aggregate's own (null-keeping) reduction is discarded. A wrapper over anything
    else passes its child through unchanged."""
    inner = expr.this
    if isinstance(inner, exp.ArrayAgg):
        return _array_agg_value(inner, ignore_nulls=True)
    return kids[0] if kids else _UNKNOWN


def _array_agg_core(
    expr: exp.AggFunc, _child: Annotation[ArrayNonEmpty]
) -> Annotation[ArrayNonEmpty]:
    """The bare ``ARRAY_AGG`` (no ``IGNORE NULLS`` wrapper). Some dialects also spell
    the null-dropping form with a ``nulls_excluded`` flag on the call itself, so read
    it here as well as via the wrapper rule."""
    return _array_agg_value(expr, ignore_nulls=bool(expr.args.get("nulls_excluded")))


def _filter_rule(
    expr: Expr, kids: tuple[Annotation[ArrayNonEmpty], ...], _ctx: DepContext
) -> Annotation[ArrayNonEmpty]:
    """``ARRAY_AGG(expr) FILTER (WHERE cond)`` parses as a ``Filter`` wrapping the aggregate.
    The filter keeps only the rows that match ``cond``, so a group whose rows all fail it
    collapses to an empty array. The filtered aggregate therefore carries no non-emptiness
    guarantee, whatever the inner (unfiltered) reduction proved. A ``Filter`` over anything
    else passes its child through unchanged."""
    inner = expr.this
    if isinstance(inner, exp.ArrayAgg) or (
        isinstance(inner, exp.IgnoreNulls) and isinstance(inner.this, exp.ArrayAgg)
    ):
        return _UNKNOWN
    return kids[0] if kids else _UNKNOWN


def _ground(_col: ColumnRef) -> Annotation[ArrayNonEmpty]:
    """Nothing is declared non-empty: every column grounds to the implicit top, and
    the transfers supply the only positive facts. A base-relation array column lands
    here too, so its emptiness stays an unknown ingestion fact."""
    return _UNKNOWN


array_nonemptiness: Property[ArrayNonEmpty, ColumnRef] = column_property(
    name="array_nonemptiness",
    lattice=ARRAY_NONEMPTY_LATTICE,
    operators={
        exp.Array: _intrinsic_constructor_rule(array_literal_nonempty),
        exp.GenerateSeries: _intrinsic_constructor_rule(generator_provably_nonempty),
        exp.GenerateDateArray: _intrinsic_constructor_rule(generator_provably_nonempty),
        exp.GenerateTimestampArray: _intrinsic_constructor_rule(generator_provably_nonempty),
        exp.IgnoreNulls: _ignore_nulls_rule,
        exp.Filter: _filter_rule,
    },
    aggregates={exp.ArrayAgg: AggregateRule(core=_array_agg_core)},
    ground=_ground,
)
