"""Pattern queries and structural hazard detectors over a parsed SQL AST.

Two layers, both pure functions over a `ParsedSQL`:

* **List queries** (`list_joins`, `list_windows`, `list_group_bys`,
  `list_aggregations`) summarise structural features of the statement. They
  return dblect-shaped value types so downstream consumers don't need to
  import sqlglot.
* **Detectors** (`detect_*`) emit `Finding`s for structural hazards: NULL
  groups after outer joins, COALESCE on a join key, undefined-ordering window
  functions and aggregates.

`scan_all` runs every detector and returns the combined findings.

The static analyser doesn't have type information, lineage, or runtime data,
so detectors prefer false positives over false negatives: each finding is a
"look at this" that a typed contract can suppress (or the user can dismiss
with the per-finding ignore syntax).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import _sqlglot as sg
from dblect.sql.parse import ParsedSQL


class FindingKind(StrEnum):
    NULL_GROUP_AFTER_OUTER_JOIN = "null_group_after_outer_join"
    COALESCE_ON_JOIN_KEY = "coalesce_on_join_key"
    UNORDERED_RANKING_WINDOW = "unordered_ranking_window"
    UNORDERED_AGGREGATE = "unordered_aggregate"


class JoinSide(StrEnum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"
    CROSS = "cross"


@dataclass(frozen=True, slots=True)
class Finding:
    """A single static-analysis observation about a SQL statement.

    ``line_start`` and ``line_end`` are 1-indexed line numbers in the SQL
    the detector was given. When that SQL came from a dbt model's
    ``raw_code``, they also correspond to lines in the user's ``.sql`` file
    (the Jinja-redacting parser preserves line counts).

    A value of ``0`` means we couldn't pin the finding to a line, which
    happens when the offending AST node has no ``Identifier`` descendants
    sqlglot stamped with position info. Callers can treat ``0`` as "model
    scope, line unknown" and report it without a line number.
    """

    kind: FindingKind
    message: str
    sql_snippet: str
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class JoinSummary:
    """One JOIN in the statement, normalised across SQL flavours."""

    side: JoinSide
    left_table: str | None
    right_table: str
    on_sql: str | None


@dataclass(frozen=True, slots=True)
class WindowSummary:
    """One window-function invocation, with its partition/order context."""

    function: str
    is_ranking: bool
    partition_by: tuple[str, ...]
    order_by: tuple[str, ...]
    sql_snippet: str


@dataclass(frozen=True, slots=True)
class GroupBySummary:
    """One GROUP BY clause, broken into its target expressions."""

    targets: tuple[str, ...]
    target_columns: tuple[tuple[str | None, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AggregateSummary:
    """One aggregate function call (SUM, COUNT, ...) not appearing as a window."""

    function: str
    argument_sql: str
    sql_snippet: str


_RANKING_FUNCTIONS: frozenset[type[Expr]] = frozenset(
    {
        exp.RowNumber,
        exp.Rank,
        exp.DenseRank,
        exp.PercentRank,
        exp.CumeDist,
        exp.Ntile,
        exp.Lag,
        exp.Lead,
        exp.FirstValue,
        exp.LastValue,
        exp.NthValue,
    }
)

_ORDERED_AGGREGATE_FUNCTIONS: frozenset[type[Expr]] = frozenset({exp.ArrayAgg, exp.GroupConcat})


def _finding_at(kind: FindingKind, *, message: str, node: Expr) -> Finding:
    """Build a Finding whose snippet and source span both come from `node`."""
    span = sg.line_range(node)
    line_start, line_end = span if span is not None else (0, 0)
    return Finding(
        kind=kind,
        message=message,
        sql_snippet=sg.render_sql(node),
        line_start=line_start,
        line_end=line_end,
    )


def list_joins(parsed: ParsedSQL) -> tuple[JoinSummary, ...]:
    """Return every JOIN in the statement, including those nested in CTEs/subqueries."""
    out: list[JoinSummary] = []
    for sel in sg.find_all_selects(parsed.tree):
        from_ = sg.from_of(sel)
        left: str | None = (
            sg.name_of(from_.this) if from_ is not None and from_.this is not None else None
        )
        for j in sg.joins_of(sel):
            on = sg.on_of(j)
            out.append(
                JoinSummary(
                    side=_join_side(j),
                    left_table=left,
                    right_table=sg.name_of(j.this),
                    on_sql=sg.render_sql(on) if on is not None else None,
                )
            )
            left = sg.name_of(j.this)
    return tuple(out)


def list_windows(parsed: ParsedSQL) -> tuple[WindowSummary, ...]:
    """Return every windowed function invocation in the statement."""
    out: list[WindowSummary] = []
    for w in sg.find_all_windows(parsed.tree):
        fn = sg.fn_of(w)
        partition = sg.partition_of(w)
        out.append(
            WindowSummary(
                function=type(fn).__name__ if fn is not None else "",
                is_ranking=type(fn) in _RANKING_FUNCTIONS if fn is not None else False,
                partition_by=tuple(sg.render_sql(e) for e in partition),
                order_by=_order_targets(sg.order_of(w)),
                sql_snippet=sg.render_sql(w),
            )
        )
    return tuple(out)


def list_group_bys(parsed: ParsedSQL) -> tuple[GroupBySummary, ...]:
    """Return every GROUP BY clause's targets, one summary per containing SELECT."""
    out: list[GroupBySummary] = []
    for sel in sg.find_all_selects(parsed.tree):
        g = sg.group_of(sel)
        if g is None:
            continue
        targets = tuple(sg.render_sql(e) for e in g.expressions)
        cols: list[tuple[str | None, str]] = []
        for e in g.expressions:
            cols.extend((sg.column_table(c), sg.column_name(c)) for c in sg.find_columns(e))
        out.append(GroupBySummary(targets=targets, target_columns=tuple(cols)))
    return tuple(out)


def list_aggregations(parsed: ParsedSQL) -> tuple[AggregateSummary, ...]:
    """Return every non-windowed aggregate-function call in the statement."""
    out: list[AggregateSummary] = []
    for node in sg.find_all_aggfunc(parsed.tree):
        if isinstance(node.parent, exp.Window):
            continue
        arg = node.this
        out.append(
            AggregateSummary(
                function=type(node).__name__,
                argument_sql=sg.render_sql(arg) if arg is not None else "",
                sql_snippet=sg.render_sql(node),
            )
        )
    return tuple(out)


def detect_null_group_after_outer_join(parsed: ParsedSQL) -> tuple[Finding, ...]:
    """Flag GROUP BY targets that reference the nullable side of an outer join.

    LEFT JOIN makes the right side's columns NULL for unmatched left rows;
    grouping by such a column collapses every unmatched left row into a
    single NULL bucket, which is almost never intended. RIGHT and FULL OUTER
    are flagged symmetrically.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(parsed.tree):
        nullable = _nullable_tables(sel)
        if not nullable:
            continue
        g = sg.group_of(sel)
        if g is None:
            continue
        for grp_expr in g.expressions:
            risky: set[str] = set()
            for c in sg.find_columns(grp_expr):
                table = sg.column_table(c)
                if table is not None and table in nullable:
                    risky.add(table)
            if risky:
                tables = ", ".join(sorted(risky))
                out.append(
                    _finding_at(
                        FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
                        message=(
                            f"GROUP BY {sg.render_sql(grp_expr)} references column(s) from "
                            f"nullable join side ({tables}); unmatched rows collapse into a NULL group"
                        ),
                        node=grp_expr,
                    )
                )
    return tuple(out)


def detect_coalesce_on_join_key(parsed: ParsedSQL) -> tuple[Finding, ...]:
    """Flag COALESCE applied to a column that also appears in a JOIN ON clause.

    Patching a join-key column with COALESCE typically defeats the NULL
    semantics that distinguish "no match" from "match with NULL". Worth a look.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(parsed.tree):
        keys: set[tuple[str | None, str]] = set()
        for j in sg.joins_of(sel):
            on = sg.on_of(j)
            if on is None:
                continue
            for c in sg.find_columns(on):
                keys.add((sg.column_table(c), sg.column_name(c)))
        if not keys:
            continue
        for coalesce in sg.find_all_coalesce(sel):
            first = coalesce.this
            if not isinstance(first, exp.Column):
                continue
            if (sg.column_table(first), sg.column_name(first)) in keys:
                out.append(
                    _finding_at(
                        FindingKind.COALESCE_ON_JOIN_KEY,
                        message=(
                            f"COALESCE on join key {sg.render_sql(first)} masks NULLs that "
                            "the JOIN's semantics distinguish"
                        ),
                        node=coalesce,
                    )
                )
    return tuple(out)


def detect_unordered_window(parsed: ParsedSQL) -> tuple[Finding, ...]:
    """Flag ranking window functions with no deterministic ORDER BY.

    ``ROW_NUMBER()``, ``RANK()``, and friends produce different results across
    runs unless an ORDER BY pins the order. ``LAG``/``LEAD``/``FIRST_VALUE``/
    ``LAST_VALUE`` are similarly meaningless without an ordering.
    """
    out: list[Finding] = []
    for w in sg.find_all_windows(parsed.tree):
        fn = sg.fn_of(w)
        if fn is None or type(fn) not in _RANKING_FUNCTIONS:
            continue
        order = sg.order_of(w)
        if order is None or not order.expressions:
            out.append(
                _finding_at(
                    FindingKind.UNORDERED_RANKING_WINDOW,
                    message=(
                        f"{type(fn).__name__.upper()} window function has no ORDER BY; "
                        "result is non-deterministic"
                    ),
                    node=w,
                )
            )
    return tuple(out)


def detect_unordered_aggregate(parsed: ParsedSQL) -> tuple[Finding, ...]:
    """Flag order-sensitive aggregates (ARRAY_AGG, STRING_AGG) with no ORDER BY."""
    out: list[Finding] = []
    for node in parsed.tree.find_all(*_ORDERED_AGGREGATE_FUNCTIONS):
        if isinstance(node.parent, exp.WithinGroup):
            continue
        inner = node.this
        if isinstance(inner, exp.Order):
            continue
        out.append(
            _finding_at(
                FindingKind.UNORDERED_AGGREGATE,
                message=(
                    f"{type(node).__name__.upper()} has no ORDER BY; "
                    "element order across rows is undefined"
                ),
                node=node,
            )
        )
    return tuple(out)


_ALL_DETECTORS = (
    detect_null_group_after_outer_join,
    detect_coalesce_on_join_key,
    detect_unordered_window,
    detect_unordered_aggregate,
)


def scan_all(parsed: ParsedSQL) -> tuple[Finding, ...]:
    """Run every detector and return findings in detector-declaration order."""
    return tuple(f for detector in _ALL_DETECTORS for f in detector(parsed))


def all_findings(parseds: Iterable[ParsedSQL]) -> tuple[Finding, ...]:
    """Convenience: scan a batch of parsed statements."""
    return tuple(f for p in parseds for f in scan_all(p))


def _join_side(j: exp.Join) -> JoinSide:
    side = sg.side_of(j)
    kind = sg.kind_of(j)
    if "CROSS" in kind:
        return JoinSide.CROSS
    match side:
        case "LEFT":
            return JoinSide.LEFT
        case "RIGHT":
            return JoinSide.RIGHT
        case "FULL":
            return JoinSide.FULL
        case _:
            return JoinSide.INNER


def _order_targets(order: exp.Order | None) -> tuple[str, ...]:
    if order is None:
        return ()
    return tuple(sg.render_sql(e) for e in order.expressions)


def _nullable_tables(sel: exp.Select) -> set[str]:
    from_ = sg.from_of(sel)
    if from_ is None:
        return set()
    nullable: set[str] = set()
    accumulated_left: set[str] = {sg.name_of(from_.this)} if from_.this is not None else set()
    for j in sg.joins_of(sel):
        right_name = sg.name_of(j.this)
        side = _join_side(j)
        if side is JoinSide.LEFT:
            nullable.add(right_name)
        elif side is JoinSide.RIGHT:
            nullable.update(accumulated_left)
        elif side is JoinSide.FULL:
            nullable.add(right_name)
            nullable.update(accumulated_left)
        accumulated_left.add(right_name)
    return nullable
