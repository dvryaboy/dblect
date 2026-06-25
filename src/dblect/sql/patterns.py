"""Pattern queries and structural hazard detectors over a parsed SQL AST.

Two layers, both pure functions over a sqlglot ``Expr``:

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

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import _sqlglot as sg
from dblect.sql._sqlglot import JoinSide

if TYPE_CHECKING:
    # Referenced only in ``suppression_hint``'s signature; imported under
    # ``TYPE_CHECKING`` so this SQL-layer module stays free of a declaration-check
    # import at load time.
    from dblect.check.findings import CheckFindingKind


class FindingKind(StrEnum):
    NULL_GROUP_AFTER_OUTER_JOIN = "null_group_after_outer_join"
    COALESCE_ON_JOIN_KEY = "coalesce_on_join_key"
    UNORDERED_RANKING_WINDOW = "unordered_ranking_window"
    UNORDERED_AGGREGATE = "unordered_aggregate"
    WHERE_ON_OUTER_JOINED_NULLABLE = "where_on_outer_joined_nullable"
    NON_DETERMINISTIC_FUNCTION = "non_deterministic_function"
    NON_UNIQUE_WINDOW_ORDER_KEYS = "non_unique_window_order_keys"
    JOIN_FANOUT = "join_fanout"
    NULL_GROUP_ON_NULLABLE_KEY = "null_group_on_nullable_key"
    JOIN_ON_NULLABLE_KEY = "join_on_nullable_key"
    NOT_IN_NULLABLE_SUBQUERY = "not_in_nullable_subquery"
    SNAPSHOT_TEMPORAL_FILTER_MISSING = "snapshot_temporal_filter_missing"


def suppression_code(kind: FindingKind | CheckFindingKind) -> str:
    """The SQLFluff-style noqa code for a finding kind: ``DBLECT_`` plus the kind's
    value uppercased (e.g. ``DBLECT_JOIN_FANOUT``). The ``DBLECT_`` prefix is what
    distinguishes our codes from real lint rule codes (``RF01`` and friends), so dbt
    lint's noqa directives and ours coexist in one comment without colliding."""
    return f"DBLECT_{kind.value.upper()}"


def suppression_hint(kind: FindingKind | CheckFindingKind) -> str:
    # The suggested directive must stay valid suppression syntax (round-trip tested).
    # Both finding families share the directive, so the hint takes either kind and
    # renders the noqa code the scanner reads back.
    return f"If this is intentional, suppress it with `-- noqa: {suppression_code(kind)}`."


@dataclass(frozen=True, slots=True)
class Finding:
    """A single static-analysis observation about a SQL statement.

    ``line_start`` and ``line_end`` are 1-indexed line numbers in the SQL
    the detector was given — the model's ``compiled_code``, which dbt
    renders with refs and macro calls expanded inline. Line numbers
    correspond to the compiled output; the reporter still surfaces the
    model's source file path so navigation works as expected.

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

_NULL_INTOLERANT_COMPARISONS: frozenset[type[Expr]] = frozenset(
    {
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.LT,
        exp.GTE,
        exp.LTE,
        exp.In,
        exp.Like,
        exp.ILike,
        exp.Between,
    }
)

_NON_DETERMINISTIC_TYPED: frozenset[type[Expr]] = frozenset(
    {
        exp.CurrentTimestamp,
        exp.CurrentDate,
        exp.CurrentTime,
        exp.CurrentDatetime,
        exp.CurrentUser,
        exp.SessionUser,
        exp.Rand,
        exp.Uuid,
    }
)

# Function names that arrive as `exp.Anonymous` rather than a dedicated sqlglot
# type and whose value changes per run (e.g. `now`). This is the portable baseline:
# names that read the same across dialects. A target adapter extends it with its
# own builtins (see `AdapterProfile.non_deterministic_builtins`), and the resolved
# set is handed to `make_non_determinism_detector`. Matched case-insensitively, so
# every entry must be lowercase.
PORTABLE_NON_DETERMINISTIC_BUILTINS: frozenset[str] = frozenset(
    {"now", "current_database", "current_schema", "gen_random_uuid", "sysdate"}
)


# --- surrogate-hash grammar --------------------------------------------------
#
# The typed-node vocabulary for recognizing a surrogate-hash key: a hash of a
# structural combination of columns. These are SQL-grammar facts (the `exp.*`
# classes are dialect-independent even though the per-dialect parser picks them),
# so they live here rather than in the uniqueness property. An adapter that hashes
# via a function sqlglot parses to `exp.Anonymous` would compose a name set on top,
# as the non-determinism builtins do; nothing demands that yet.
#
# These are tuples, not frozensets like the sets above, because membership is
# tested with `isinstance`, whose subclass-awareness is load-bearing: `TO_HEX(...)`
# parses to `exp.LowerHex`, a subclass of `exp.Hex`, so listing `Hex` looks through
# the hex wrapper. A hash's hex and raw-digest spellings, though, are siblings, not
# in a subclass relation (`MD5`/`MD5Digest`, `SHA2`/`SHA2Digest`), so both are
# listed explicitly. Resolved by name for tolerance across sqlglot versions.
SURROGATE_HASH_FUNCTIONS: tuple[type[Expr], ...] = tuple(
    getattr(exp, n)
    for n in ("MD5", "MD5Digest", "SHA", "SHA1Digest", "SHA2", "SHA2Digest", "FarmFingerprint")
    if hasattr(exp, n)
)
# Single-argument wrappers that do not change which tuple is hashed, looked through
# to reach the hash (e.g. `TO_HEX(MD5(...))`, `LOWER(...)`).
SURROGATE_HASH_PASSTHROUGH: tuple[type[Expr], ...] = tuple(
    getattr(exp, n) for n in ("Hex", "Lower", "Upper") if hasattr(exp, n)
)
# Structural combinators that assemble columns into the hashed value without making
# the input anything other than those columns.
SURROGATE_HASH_STRUCTURAL: tuple[type[Expr], ...] = tuple(
    getattr(exp, n)
    for n in ("Concat", "DPipe", "Cast", "TryCast", "Coalesce", "Lower", "Upper", "Trim", "Paren")
    if hasattr(exp, n)
)


def finding_at(kind: FindingKind, *, message: str, node: Expr) -> Finding:
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


def list_joins(tree: Expr) -> tuple[JoinSummary, ...]:
    """Return every JOIN in the statement, including those nested in CTEs/subqueries."""
    out: list[JoinSummary] = []
    for sel in sg.find_all_selects(tree):
        from_ = sg.from_of(sel)
        left: str | None = (
            sg.name_of(from_.this) if from_ is not None and from_.this is not None else None
        )
        for j in sg.joins_of(sel):
            on = sg.on_of(j)
            out.append(
                JoinSummary(
                    side=sg.join_side_of(j),
                    left_table=left,
                    right_table=sg.name_of(j.this),
                    on_sql=sg.render_sql(on) if on is not None else None,
                )
            )
            left = sg.name_of(j.this)
    return tuple(out)


def list_windows(tree: Expr) -> tuple[WindowSummary, ...]:
    """Return every windowed function invocation in the statement."""
    out: list[WindowSummary] = []
    for w in sg.find_all_windows(tree):
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


def list_group_bys(tree: Expr) -> tuple[GroupBySummary, ...]:
    """Return every GROUP BY clause's targets, one summary per containing SELECT."""
    out: list[GroupBySummary] = []
    for sel in sg.find_all_selects(tree):
        g = sg.group_of(sel)
        if g is None:
            continue
        targets = tuple(sg.render_sql(e) for e in g.expressions)
        cols: list[tuple[str | None, str]] = []
        for e in g.expressions:
            cols.extend((sg.column_table(c), sg.column_name(c)) for c in sg.find_columns(e))
        out.append(GroupBySummary(targets=targets, target_columns=tuple(cols)))
    return tuple(out)


def list_aggregations(tree: Expr) -> tuple[AggregateSummary, ...]:
    """Return every non-windowed aggregate-function call in the statement."""
    out: list[AggregateSummary] = []
    for node in sg.find_all_aggfunc(tree):
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


def detect_null_group_after_outer_join(tree: Expr) -> tuple[Finding, ...]:
    """Flag GROUP BY targets that reference the nullable side of an outer join.

    LEFT JOIN makes the right side's columns NULL for unmatched left rows;
    grouping by such a column collapses every unmatched left row into a
    single NULL bucket, which is almost never intended. RIGHT and FULL OUTER
    are flagged symmetrically.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
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
                    finding_at(
                        FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
                        message=(
                            f"GROUP BY {sg.render_sql(grp_expr)} references column(s) from "
                            f"nullable join side ({tables}); unmatched rows collapse into a NULL group"
                        ),
                        node=grp_expr,
                    )
                )
    return tuple(out)


def detect_coalesce_on_join_key(tree: Expr) -> tuple[Finding, ...]:
    """Flag COALESCE applied to a column that also appears in a JOIN ON clause.

    Patching a join-key column with COALESCE typically defeats the NULL
    semantics that distinguish "no match" from "match with NULL". Worth a look.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
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
                    finding_at(
                        FindingKind.COALESCE_ON_JOIN_KEY,
                        message=(
                            f"COALESCE on join key {sg.render_sql(first)} masks NULLs that "
                            "the JOIN's semantics distinguish"
                        ),
                        node=coalesce,
                    )
                )
    return tuple(out)


def detect_unordered_window(tree: Expr) -> tuple[Finding, ...]:
    """Flag ranking window functions with no deterministic ORDER BY.

    ``ROW_NUMBER()``, ``RANK()``, and friends produce different results across
    runs unless an ORDER BY pins the order. ``LAG``/``LEAD``/``FIRST_VALUE``/
    ``LAST_VALUE`` are similarly meaningless without an ordering.
    """
    out: list[Finding] = []
    for w in sg.find_all_windows(tree):
        fn = sg.fn_of(w)
        if fn is None or type(fn) not in _RANKING_FUNCTIONS:
            continue
        order = sg.order_of(w)
        if order is None or not order.expressions:
            out.append(
                finding_at(
                    FindingKind.UNORDERED_RANKING_WINDOW,
                    message=(
                        f"{type(fn).__name__.upper()} window function has no ORDER BY; "
                        "result is non-deterministic"
                    ),
                    node=w,
                )
            )
    return tuple(out)


def detect_unordered_aggregate(tree: Expr) -> tuple[Finding, ...]:
    """Flag order-sensitive aggregates (ARRAY_AGG, STRING_AGG) with no ORDER BY."""
    out: list[Finding] = []
    for node in tree.find_all(*_ORDERED_AGGREGATE_FUNCTIONS):
        if isinstance(node.parent, exp.WithinGroup):
            continue
        if sg.aggregate_order_of(node) is not None:
            continue
        out.append(
            finding_at(
                FindingKind.UNORDERED_AGGREGATE,
                message=(
                    f"{type(node).__name__.upper()} has no ORDER BY; "
                    "element order across rows is undefined"
                ),
                node=node,
            )
        )
    return tuple(out)


def detect_where_on_outer_joined_nullable(tree: Expr) -> tuple[Finding, ...]:
    """Flag WHERE predicates that silently invert an OUTER JOIN into an INNER one.

    ``select * from a left join b on a.k = b.k where b.col = X`` rejects every
    unmatched row from ``a`` because ``b.col`` is NULL there and ``NULL = X``
    is NULL. The LEFT JOIN reads like it preserves left rows, but the WHERE
    quietly takes them back. The fix is to move the predicate into the JOIN's
    ON clause or to wrap the column in ``coalesce``.

    A predicate is "protected" if the column reference is wrapped in
    ``COALESCE`` or sits inside an ``IS [NOT] NULL`` check between itself and
    the comparison node. Protected predicates are silent.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        nullable = _nullable_tables(sel)
        if not nullable:
            continue
        where = sel.args.get("where")
        if where is None:
            continue
        for cmp in where.find_all(*_NULL_INTOLERANT_COMPARISONS):
            risky_tables: set[str] = set()
            for c in sg.find_columns(cmp):
                if _is_null_protected(c, until=cmp):
                    continue
                table = sg.column_table(c)
                if table is not None and table in nullable:
                    risky_tables.add(table)
            if not risky_tables:
                continue
            # A nullable-side predicate inside a top-level OR does not invert the
            # join when a sibling disjunct keeps the rows where this side is NULL
            # alive: `a.x > 0 OR b.y > 0` on a left join, or the full-outer
            # `l.v > 0 OR r.v > 0` idiom. The disjunction is join-preserving, so
            # the predicate is not the inverting culprit.
            if _rescued_by_or_disjunction(cmp, risky_tables, where):
                continue
            tables = ", ".join(sorted(risky_tables))
            out.append(
                finding_at(
                    FindingKind.WHERE_ON_OUTER_JOINED_NULLABLE,
                    message=(
                        f"WHERE predicate {sg.render_sql(cmp)} compares a column from "
                        f"nullable join side ({tables}); rows where the join didn't match "
                        "are filtered out, silently inverting the OUTER JOIN to INNER. "
                        "Move the predicate into the ON clause, or guard the column "
                        "with COALESCE / IS [NOT] NULL."
                    ),
                    node=cmp,
                )
            )
    return tuple(out)


def make_non_determinism_detector(
    non_deterministic_builtins: frozenset[str] = PORTABLE_NON_DETERMINISTIC_BUILTINS,
) -> Callable[[Expr], tuple[Finding, ...]]:
    """Build a detector flagging non-deterministic calls in load-bearing positions.

    "Load-bearing" means the function's value affects which rows go where,
    not just what gets projected. JOIN ON conditions, GROUP BY targets,
    PARTITION BY and ORDER BY inside window specs all qualify; ``WHERE`` and
    ``HAVING`` do not (incremental models that filter on
    ``now() - interval`` are a known and legitimate idiom). Projected
    non-determinism (``select current_timestamp() as loaded_at``) is also
    silent.

    Functions: ``current_timestamp``, ``current_date``, ``current_time``,
    ``current_user``, ``random()``, ``uuid()``/``gen_random_uuid()``,
    ``now()``, plus the target adapter's own builtins. Typed forms match by
    sqlglot expression type; ``exp.Anonymous`` calls match by name
    (case-insensitive) against ``non_deterministic_builtins``. That set is
    adapter-bound (``AdapterProfile.non_deterministic_builtins``), so this is a
    ``make_*`` factory the audit builds per run rather than a bare structural
    detector; it defaults to the portable baseline for standalone use.

    This static check is a fast pre-filter over a curated, necessarily incomplete
    list; the runtime replay-determinism loop is the completeness layer.

    TODO: once we read ``Node.config.materialized``, narrow the detector to
    table/incremental models (the cases where stored aggregates drift over
    time). Views are recomputed every read, so the hazard mostly evaporates.
    """

    def detect(tree: Expr) -> tuple[Finding, ...]:
        return tuple(
            finding_at(
                FindingKind.NON_DETERMINISTIC_FUNCTION,
                message=(
                    f"{_non_deterministic_name(call)} appears in {label}; this position is "
                    "load-bearing because the value affects which rows go where (filtering, "
                    "grouping, ranking). Output buckets drift with wall-clock time. If "
                    "intentional, suppress with "
                    f"`-- noqa: {suppression_code(FindingKind.NON_DETERMINISTIC_FUNCTION)}`; "
                    "if not, bucket by the absolute timestamp and derive the relative measure "
                    "at query time."
                ),
                node=scope,
            )
            for sel in sg.find_all_selects(tree)
            for label, scope in _load_bearing_scopes(sel)
            for call in _find_non_deterministic(scope, non_deterministic_builtins)
        )

    return detect


# The dialect-agnostic structural detectors. The non-determinism detector is
# adapter-bound, so it is built per run by `make_non_determinism_detector` and
# composed in separately (see `scan_all` and the audit walker).
_STRUCTURAL_DETECTORS = (
    detect_null_group_after_outer_join,
    detect_coalesce_on_join_key,
    detect_unordered_window,
    detect_unordered_aggregate,
    detect_where_on_outer_joined_nullable,
)


def scan_all(
    tree: Expr,
    *,
    non_deterministic_builtins: frozenset[str] = PORTABLE_NON_DETERMINISTIC_BUILTINS,
) -> tuple[Finding, ...]:
    """Run every detector and return findings in detector-declaration order.

    The non-determinism check uses ``non_deterministic_builtins`` (the portable
    baseline by default; the audit passes the resolved adapter's set).
    """
    structural = (f for detector in _STRUCTURAL_DETECTORS for f in detector(tree))
    non_determinism = make_non_determinism_detector(non_deterministic_builtins)(tree)
    return (*structural, *non_determinism)


def all_findings(
    trees: Iterable[Expr],
    *,
    non_deterministic_builtins: frozenset[str] = PORTABLE_NON_DETERMINISTIC_BUILTINS,
) -> tuple[Finding, ...]:
    """Convenience: scan a batch of parsed statements."""
    return tuple(
        f for t in trees for f in scan_all(t, non_deterministic_builtins=non_deterministic_builtins)
    )


def _order_targets(order: exp.Order | None) -> tuple[str, ...]:
    if order is None:
        return ()
    return tuple(sg.render_sql(e) for e in order.expressions)


def _nullable_tables(sel: exp.Select) -> set[str]:
    return sg.outer_join_optional_aliases(sel)


def _is_null_protected(col: exp.Column, *, until: Expr) -> bool:
    """True if `col` is wrapped in a NULL-tolerant context before reaching `until`.

    Walks from `col` up the AST. ``COALESCE(col, ...)`` returns the fallback
    when ``col`` is NULL, so the comparison sees a non-NULL value. ``IS NULL``
    and ``IS NOT NULL`` are themselves null checks, so the analyst is
    explicitly handling the nullable case.
    """
    node: Expr | None = col
    while node is not None and node is not until:
        if isinstance(node, exp.Coalesce | exp.Is):
            return True
        node = node.parent
    return False


def _top_level_disjuncts(predicate: Expr) -> list[Expr]:
    """Flatten the top-level OR tree of `predicate` into its disjuncts. A WHERE
    that is not an OR at its root yields a single disjunct (itself), so callers
    see no disjunction to reason about."""
    out: list[Expr] = []
    stack: list[Expr] = [predicate]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Or):
            stack.extend((node.this, node.expression))
        else:
            out.append(node)
    return out


def _is_descendant(node: Expr, ancestor: Expr) -> bool:
    cur: Expr | None = node
    while cur is not None:
        if cur is ancestor:
            return True
        cur = cur.parent
    return False


def _rescued_by_or_disjunction(cmp: Expr, risky: set[str], where: exp.Where) -> bool:
    """True if `cmp` is one term of a top-level OR whose other side keeps the
    rows where `cmp`'s nullable tables are NULL alive, so `cmp` does not invert
    the outer join.

    A sibling disjunct rescues when it references at least one column and none of
    its columns come from a table in `risky`: it can hold for rows where those
    tables did not match (a predicate on the preserved side, or on the other side
    of a full outer join). A disjunct that also constrains a risky table, or one
    with no column at all (a bare literal), does not qualify, so the genuine
    inversions (a lone predicate, an AND, or an OR over the same nullable side)
    still fire.
    """
    disjuncts = _top_level_disjuncts(where.this)
    if len(disjuncts) <= 1:
        return False
    own = next((d for d in disjuncts if _is_descendant(cmp, d)), None)
    if own is None:
        return False
    for d in disjuncts:
        if d is own:
            continue
        cols = sg.find_columns(d)
        if cols and all(sg.column_table(c) not in risky for c in cols):
            return True
    return False


def _load_bearing_scopes(sel: exp.Select) -> list[tuple[str, Expr]]:
    """Locations in `sel` where non-determinism changes which rows go where.

    Returns ``(label, scope)`` pairs:

    * Each JOIN's ON clause.
    * Each GROUP BY target expression.
    * Each window function's PARTITION BY expression list (rolled up as the
      window node itself for snippet purposes) and ORDER BY expression list.

    WHERE and HAVING are intentionally absent: a 7-day lookback like
    ``where ts >= now() - interval`` is a legitimate incremental idiom that
    we don't want to flag.
    """
    scopes: list[tuple[str, Expr]] = []
    for j in sg.joins_of(sel):
        on = sg.on_of(j)
        if on is not None:
            scopes.append(("a JOIN ON clause", on))
    group = sg.group_of(sel)
    if group is not None:
        scopes.extend(("a GROUP BY target", g) for g in group.expressions)
    for w in sg.find_all_windows(sel):
        scopes.extend(("a window PARTITION BY", part) for part in sg.partition_of(w))
        order = sg.order_of(w)
        if order is not None:
            scopes.extend(("a window ORDER BY", e) for e in order.expressions)
    return scopes


def _find_non_deterministic(e: Expr, names: frozenset[str]) -> list[Expr]:
    """Every non-deterministic function call reachable from `e` (transitive)."""
    return [node for node in e.walk() if _is_non_deterministic(node, names)]


def _is_non_deterministic(node: Expr, names: frozenset[str]) -> bool:
    if type(node) in _NON_DETERMINISTIC_TYPED:
        return True
    return (
        isinstance(node, exp.Anonymous)
        and isinstance(node.this, str)
        and node.this.lower() in names
    )


def _non_deterministic_name(call: Expr) -> str:
    if isinstance(call, exp.Anonymous) and isinstance(call.this, str):
        return f"{call.this}()"
    return type(call).__name__
