"""Whether a window's result survives every way of breaking ties in its ORDER BY.

The decision is a table over function kind (:class:`OrderUse`) and a test on the frame. A frame
is *tie closed* when it takes a tied row's peers together or the whole partition: ``RANGE`` and
``GROUPS`` frames, the default frame, and ``ROWS`` over the whole partition. A ``ROWS`` frame
anchored at the current row moves with the row's position among its ties. ``EXCLUDE`` removes
rows by identity or peer group, so it leaves a tie closed frame tie closed.

* ``PEER_RANK``: independent, whatever the frame.
* ``NONE``: independent when the frame is tie closed, since a tie closed frame holds the same
  rows for every tie order.
* ``SEQUENCE``: independent when the frame is tie closed and the order determines every column
  the call reads. The sorted sequence of values the order determines is the same for every tie
  order, so first, last, nth and the collected array are fixed.
* ``LAST_VALUE`` over a ``ROWS`` frame ending at the current row is the current row's own
  value, and so is ``FIRST_VALUE`` over a frame starting there, provided nothing is excluded
  and nulls are not skipped.
* ``POSITION`` and ``PICKS``: never. ``LAG(x) OVER (ORDER BY x)`` still moves: with rows
  ``x = 1, 5, 5, 7`` the first 5 sees 1 or 5 depending on which tied row sorts first.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

import sqlglot.expressions as exp

from dblect.sql import _sqlglot as sg
from dblect.sql.aggregates import aggregate_order_use
from dblect.sql.order_use import OrderUse
from dblect.sql.patterns import contains_per_row_volatile

__all__ = ["window_order_use", "window_tie_independent"]

# Window-only functions; an aggregate used as a window function reads its registry entry.
_WINDOW_FUNCTIONS: Mapping[type[exp.Expr], OrderUse] = {
    exp.Rank: OrderUse.PEER_RANK,
    exp.DenseRank: OrderUse.PEER_RANK,
    exp.PercentRank: OrderUse.PEER_RANK,
    exp.CumeDist: OrderUse.PEER_RANK,
    exp.RowNumber: OrderUse.POSITION,
    exp.Ntile: OrderUse.POSITION,
    exp.Lag: OrderUse.POSITION,
    exp.Lead: OrderUse.POSITION,
    exp.FirstValue: OrderUse.SEQUENCE,
    exp.LastValue: OrderUse.SEQUENCE,
    exp.NthValue: OrderUse.SEQUENCE,
}

# Nodes that bring rows or an order of their own. Calls that differ per row are matched by
# `contains_per_row_volatile`.
_UNDETERMINED_NODES = (exp.Select, exp.Subquery, exp.Order, exp.Window)


class _FrameKind(StrEnum):
    ROWS = "rows"
    RANGE = "range"
    GROUPS = "groups"


def _unwrapped(fn: exp.Expr) -> exp.Expr:
    """The call under a ``RESPECT NULLS`` / ``IGNORE NULLS`` wrapper. Skipping nulls drops
    entries from the sorted sequence without reordering it."""
    while isinstance(fn, (exp.IgnoreNulls, exp.RespectNulls)):
        fn = fn.this
    return fn


def window_order_use(fn: exp.Expr) -> OrderUse:
    """How the window function ``fn`` reads row order."""
    fn = _unwrapped(fn)
    for cls in type(fn).__mro__:
        use = _WINDOW_FUNCTIONS.get(cls)
        if use is not None:
            return use
    if isinstance(fn, exp.AggFunc):
        return aggregate_order_use(fn)
    return OrderUse.PICKS


def _is_unbounded(bound: object, side: object, direction: str) -> bool:
    return (
        isinstance(bound, str)
        and bound.upper() == "UNBOUNDED"
        and isinstance(side, str)
        and side.lower() == direction
    )


def _frame_kind(spec: exp.Expr) -> _FrameKind | None:
    try:
        return _FrameKind(str(spec.args.get("kind") or "range").lower())
    except ValueError:
        return None


def _frame_is_tie_closed(window: exp.Window) -> bool:
    spec = window.args.get("spec")
    if spec is None:
        return True  # the default frame is RANGE, which takes peers together
    match _frame_kind(spec):
        case _FrameKind.RANGE | _FrameKind.GROUPS:
            return True
        case _FrameKind.ROWS:
            return _is_unbounded(
                spec.args.get("start"), spec.args.get("start_side"), "preceding"
            ) and _is_unbounded(spec.args.get("end"), spec.args.get("end_side"), "following")
        case None:
            return False


def _is_current_row(bound: object) -> bool:
    return isinstance(bound, str) and bound.upper() == "CURRENT ROW"


def _edge_is_current_row(window: exp.Window) -> bool:
    """Whether the frame's last row (for ``LAST_VALUE``) or first row (for ``FIRST_VALUE``)
    is the current row itself, with no row excluded. Only ``ROWS`` frames qualify: a ``RANGE``
    or ``GROUPS`` edge at the current row reaches the whole peer group."""
    spec = window.args.get("spec")
    if spec is None or _frame_kind(spec) is not _FrameKind.ROWS:
        return False
    exclude = spec.args.get("exclude")
    if exclude is not None and exclude.name.upper() != "NO OTHERS":
        return False
    if isinstance(window.this, exp.LastValue):
        end = spec.args.get("end")
        return end is None or _is_current_row(end)  # an omitted end is the current row
    if isinstance(window.this, exp.FirstValue):
        return _is_current_row(spec.args.get("start"))
    return False


def _args_are_stable(fn: exp.Expr) -> bool:
    """Whether nothing in ``fn``'s arguments brings rows, an order, or a per-row volatile call
    that could differ between evaluations for the same row."""
    return not any(
        arg.find(*_UNDETERMINED_NODES) is not None or contains_per_row_volatile(arg)
        for arg in fn.iter_expressions()
    )


def _reads_only(fn: exp.Expr, determined: frozenset[str]) -> bool:
    """Whether every column ``fn`` reads is in ``determined`` and nothing in its arguments
    can differ between evaluations for the same row."""
    return _args_are_stable(fn) and all(
        sg.column_name(col) in determined
        for arg in fn.iter_expressions()
        for col in arg.find_all(exp.Column)
    )


def window_tie_independent(window: exp.Window, determined: frozenset[str]) -> bool:
    """Whether every row's value from ``window`` is the same for every tie order.

    ``determined`` is the set of columns the window's ORDER BY and PARTITION BY fix (their
    closure under the source's dependencies). A window with no ORDER BY has no ties to break
    and is not asked.
    """
    fn = _unwrapped(window.this)
    match window_order_use(fn):
        case OrderUse.PEER_RANK:
            return True
        case OrderUse.NONE:
            return _frame_is_tie_closed(window)
        case OrderUse.SEQUENCE:
            if _edge_is_current_row(window):
                return _args_are_stable(fn)
            return _frame_is_tie_closed(window) and _reads_only(fn, determined)
        case OrderUse.POSITION | OrderUse.PICKS:
            return False
