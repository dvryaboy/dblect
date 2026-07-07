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

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import _sqlglot as sg
from dblect.sql import guards
from dblect.sql._sqlglot import JoinSide
from dblect.sql.findings import Finding, FindingKind, finding_at, suppression_code
from dblect.sql.vocab import array_literal_nonempty, generator_provably_nonempty


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

# Array-flattening constructs whose inner (non-outer) form drops a parent row when the
# array is empty or NULL. ``UNNEST`` and ``explode``/``flatten`` parse to dedicated types
# across dialects (duckdb/bigquery ``UNNEST`` -> ``exp.Unnest``; snowflake ``flatten`` and
# spark ``explode`` -> ``exp.Explode``); a dialect that leaves the flatten anonymous is
# matched by name. Resolved by ``isinstance`` so subclasses look through.
_ARRAY_FLATTEN_TYPED: tuple[type[Expr], ...] = tuple(
    getattr(exp, n) for n in ("Unnest", "Explode", "Posexplode", "Inline") if hasattr(exp, n)
)
_ARRAY_FLATTEN_NAMES: frozenset[str] = frozenset(
    {"unnest", "explode", "explode_outer", "posexplode", "flatten", "json_array_elements"}
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
            cols.extend(sg.column_key(c) for c in sg.find_columns(e))
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

    A nullable-side column is cleared when the value-effect catalog proves the
    grouped value cannot be the padding NULL: an ``IS [NOT] NULL`` test (the
    buckets are the two booleans), or a ``COALESCE`` whose fallback the join keeps
    present (``coalesce(meta.key, base.key)`` recovers the preserved-side key). A
    ``COALESCE`` of two nullable sides still fires: the merged key can be NULL.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        nullable = _nullable_tables(sel)
        if not nullable:
            continue
        g = sg.group_of(sel)
        if g is None:
            continue
        nullable_fs = frozenset(nullable)
        for grp_expr in g.expressions:
            risky: set[str] = set()
            for c in sg.find_columns(grp_expr):
                table = sg.column_table(c)
                if table is None or table not in nullable:
                    continue
                if guards.is_null_checked(c, until=grp_expr) or guards.supplies_present_value(
                    c, until=grp_expr, nullable=nullable_fs
                ):
                    continue
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
    """Flag COALESCE applied to a join-key column inside a JOIN ON clause.

    Patching a join-key column with COALESCE in the match condition itself
    (``on coalesce(a.k, 0) = coalesce(b.k, 0)``) defeats the NULL semantics that
    distinguish "no match" from "match with NULL", turning non-matches into
    matches on the sentinel. That is the load-bearing position for this hazard.

    The same ``coalesce(a.k, b.k)`` in the projection of a FULL/RIGHT outer join
    is the opposite: the canonical merge idiom that recovers the key from whichever
    side matched, a value-effect guard rather than a hazard. So the search is scoped
    to the ON clause and the projection-list merge stays silent (issue #139).

    The keys are the columns of the ON clause's equality predicates, the actual match
    positions. A filter pushed into the ON clause (``on a.k = b.k and coalesce(b.flag,
    true)``) contributes no key, so a COALESCE over its columns is not mistaken for a
    masked join key.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        ons = [on for j in sg.joins_of(sel) if (on := sg.on_of(j)) is not None]
        keys: set[tuple[str | None, str]] = {
            sg.column_key(c)
            for on in ons
            for eq in on.find_all(exp.EQ)
            for c in sg.find_columns(eq)
        }
        if not keys:
            continue
        for on in ons:
            for coalesce in on.find_all(exp.Coalesce):
                first = coalesce.this
                if not isinstance(first, exp.Column):
                    continue
                if sg.column_key(first) in keys:
                    out.append(
                        finding_at(
                            FindingKind.COALESCE_ON_JOIN_KEY,
                            message=(
                                f"COALESCE on join key {sg.render_sql(first)} in the ON clause "
                                "masks NULLs that the JOIN's semantics distinguish"
                            ),
                            node=coalesce,
                        )
                    )
    return tuple(out)


def _partition_column_keys(w: exp.Window) -> frozenset[tuple[str | None, str]] | None:
    """The PARTITION BY columns of ``w`` as ``(table, name)`` keys.

    ``None`` when the partition is empty or any term is not a bare column: an expression like
    ``partition by lower(id)`` is one we cannot match against a projected column, so the
    caller stays conservative.
    """
    keys: set[tuple[str | None, str]] = set()
    for term in sg.partition_of(w):
        if not isinstance(term, exp.Column):
            return None
        keys.add(sg.column_key(term))
    return frozenset(keys) if keys else None


def _columns_outside_windows_covered(node: Expr, keys: frozenset[tuple[str | None, str]]) -> bool:
    """True when every column ``node`` carries forward is one of ``keys``.

    Columns inside a window spec are excluded: those are the dedup's own PARTITION BY terms,
    which are the keys by construction. A star or subquery makes coverage unprovable (its
    columns are not enumerable here), so either one is reported as not covered.
    """
    if node.find(exp.Star, exp.Subquery, exp.Select) is not None:
        return False
    for col in sg.find_columns(node):
        if col.find_ancestor(exp.Window) is not None:
            continue
        if sg.column_key(col) not in keys:
            return False
    return True


def _scope_is_aggregating(scope: exp.Select) -> bool:
    """True when ``scope`` collapses its rows: an explicit GROUP BY, or a bare aggregate that
    triggers implicit grouping (``select count(1) from src`` is one group with no GROUP BY).

    An aggregate inside the window spec or a nested subquery belongs elsewhere and does not
    count. A collapsing scope carries different rows than the per-row dedup argument assumes,
    so the caller stays conservative when this holds.
    """
    if sg.group_of(scope) is not None:
        return True
    return any(
        agg.find_ancestor(exp.Select) is scope and agg.find_ancestor(exp.Window) is None
        for agg in sg.find_all_aggfunc(scope)
    )


def _row_number_dedup_is_order_insensitive(w: exp.Window) -> bool:
    """True when an ORDER-BY-less ``row_number()`` cannot affect its query's result bag.

    A ``row_number()`` with no ORDER BY labels rows in an arbitrary within-partition order.
    That label is observable only through the columns the ranked scope carries forward. When
    every carried column is a partition key, all rows in a partition are identical on the
    surfaced columns, so the scope's output bag is the same whichever physical row each rank
    lands on: the labels ``1..n`` attach to indistinguishable rows. A consumer of a
    deterministic bag is itself deterministic, so the decision rests on the ranked scope
    alone, whether the dedup filter sits in a QUALIFY here or a ``where rn = 1`` one level out.

    Scoped to ``row_number()`` as the sole window of a non-aggregating scope, the dedup idiom
    the refinement targets. ``rank()``/``dense_rank()``/value windows, and multi-window or
    aggregating scopes (see :func:`_scope_is_aggregating`), stay flagged: the output-bag
    argument does not carry to them unchanged.
    """
    if not isinstance(sg.fn_of(w), exp.RowNumber):
        return False
    keys = _partition_column_keys(w)
    if keys is None:
        return False
    scope = w.find_ancestor(exp.Select)
    if scope is None or _scope_is_aggregating(scope):
        return False
    scope_windows = [
        win for win in sg.find_all_windows(scope) if win.find_ancestor(exp.Select) is scope
    ]
    if scope_windows != [w]:
        return False
    if not all(_columns_outside_windows_covered(proj, keys) for proj in scope.expressions):
        return False
    qualify = sg.qualify_of(scope)
    if qualify is None:
        return True
    # A QUALIFY commonly names the window by its SELECT alias (``qualify rn = 1``) instead of
    # inlining it. That reference is this window's rank label, covered by the same output-bag
    # argument as the inline form, so admit it alongside the partition keys.
    alias = sg.window_output_alias(w)
    qualify_keys = keys | {(None, alias)} if alias is not None else keys
    return _columns_outside_windows_covered(qualify.this, qualify_keys)


def detect_unordered_window(tree: Expr) -> tuple[Finding, ...]:
    """Flag ranking window functions with no deterministic ORDER BY.

    ``ROW_NUMBER()``, ``RANK()``, and friends produce different results across
    runs unless an ORDER BY pins the order. ``LAG``/``LEAD``/``FIRST_VALUE``/
    ``LAST_VALUE`` are similarly meaningless without an ordering.

    The exception is the dedup where a ``row_number() = 1`` keeps one row per partition and
    the partition key covers every column the ranked scope carries forward: the surviving
    rows are then identical on all surfaced columns, so the missing ORDER BY does not change
    the output (see :func:`_row_number_dedup_is_order_insensitive`).
    """
    out: list[Finding] = []
    for w in sg.find_all_windows(tree):
        fn = sg.fn_of(w)
        if fn is None or type(fn) not in _RANKING_FUNCTIONS:
            continue
        order = sg.order_of(w)
        if order is not None and order.expressions:
            continue
        if _row_number_dedup_is_order_insensitive(w):
            continue
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
    for node in sg.find_all_ordered_aggregates(tree):
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

    A predicate is cleared when the value-effect catalog neutralises the padding
    NULL: the column is wrapped in ``COALESCE`` or an ``IS [NOT] NULL`` check
    before the comparison, or the comparison is one term of a top-level ``OR``
    whose sibling disjunct keeps the unmatched rows alive (``where a.x > 0 or
    b.y > 0`` does not invert the join). Cleared predicates are silent.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        nullable = _nullable_tables(sel)
        if not nullable:
            continue
        where = sg.where_of(sel)
        if where is None:
            continue
        nullable_fs = frozenset(nullable)
        for cmp in where.find_all(*_NULL_INTOLERANT_COMPARISONS):
            risky_tables: set[str] = set()
            for c in sg.find_columns(cmp):
                if guards.is_coalesced(c, until=cmp) or guards.is_null_checked(c, until=cmp):
                    continue
                table = sg.column_table(c)
                if table is not None and table in nullable:
                    risky_tables.add(table)
            if not risky_tables:
                continue
            if guards.rescued_by_or_sibling(cmp, where=where, nullable=nullable_fs):
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


def detect_inner_flatten_row_drop(
    tree: Expr, *, column_is_nonempty: Callable[[exp.Column], bool] | None = None
) -> tuple[Finding, ...]:
    """Flag an inner array-flatten arm (``UNNEST``/``explode``/``flatten``) that drops the
    parent row when the array is empty or NULL.

    ``FROM t, UNNEST(t.arr)`` (equivalently ``CROSS JOIN UNNEST(...)`` or ``CROSS JOIN
    LATERAL ...``) emits zero rows for any ``t`` whose ``arr`` is empty or null, so the
    lateral behaves like an inner join against the unnested set and the parent row vanishes.
    Analysts usually expect every parent row to survive. This is the deflation twin of
    ``join_fanout``: fan-out multiplies rows, this annihilates them. The row-preserving form
    is the ``LEFT``/``OUTER`` variant (``LEFT JOIN UNNEST(...) ON TRUE``, ``LATERAL
    FLATTEN(... OUTER => TRUE)``, spark ``LATERAL VIEW OUTER explode(...)``), which is
    silent. The construct parses dialect-specifically, so the detector reads the structural
    shape sqlglot produces rather than the surface syntax.

    An ``UNNEST`` whose argument is provably non-empty drops no row, so it is silent too.
    A literal ``ARRAY[...]`` constructor with one or more elements is the local, always-on
    case (the wide-to-long pivot idiom). ``column_is_nonempty``, when supplied, answers
    whether an unnested *column* is provably non-empty; the audit builds it over the
    ``array_nonemptiness`` property resolved across model and CTE boundaries, so a
    rebuilt-then-unnested array stays quiet. The detector treats columns as opaque
    otherwise, which keeps this module free of lineage types.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        nullable = _nullable_tables(sel)
        for j in sg.joins_of(sel):
            kernel = _flatten_arm(j.this)
            if kernel is None:
                continue
            if sg.join_side_of(j) in _OUTER_JOIN_SIDES or _flatten_preserves_rows(kernel):
                continue
            if _unnest_arg_provably_nonempty(kernel, column_is_nonempty, nullable):
                continue
            out.append(_inner_flatten_finding(j))
        for lat in sg.laterals_of(sel):
            # Spark `LATERAL VIEW [OUTER] explode(...)` is a standalone lateral (view=True)
            # stored on the select; the join-arm laterals (snowflake) carry view=False and
            # are handled above as flatten arms.
            if not _is_flatten_like(lat.this):
                continue
            if _flatten_preserves_rows(lat):
                continue
            out.append(_inner_flatten_finding(lat))
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
    detect_inner_flatten_row_drop,
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


_OUTER_JOIN_SIDES: frozenset[JoinSide] = frozenset({JoinSide.LEFT, JoinSide.RIGHT, JoinSide.FULL})


def _is_flatten_like(node: Expr | None) -> bool:
    """True if ``node`` is an array-flattening expression (``UNNEST``, ``explode``,
    ``flatten``), by dedicated type or, for a dialect that leaves it anonymous, by name."""
    if node is None:
        return False
    return sg.matches_typed_or_named(node, _ARRAY_FLATTEN_TYPED, _ARRAY_FLATTEN_NAMES)


_FLATTEN_WRAPPER_TYPED: tuple[type[Expr], ...] = tuple(
    getattr(exp, n) for n in ("Lateral", "TableFromRows") if hasattr(exp, n)
)


def _flatten_arm(node: Expr) -> Expr | None:
    """The flatten kernel of a FROM/JOIN arm, or ``None`` if the arm is not an array
    flatten. ``UNNEST`` sits directly in the arm; ``LATERAL``/``TABLE(...)`` wrap the
    flatten function, and we return the wrapper so its ``OUTER`` marker can be read."""
    if isinstance(node, exp.Unnest):
        return node
    if isinstance(node, _FLATTEN_WRAPPER_TYPED) and _is_flatten_like(node.this):
        return node
    return None


def _flatten_preserves_rows(kernel: Expr) -> bool:
    """True if a flatten arm is written in a row-preserving outer form. The dialects spell
    this three ways, consolidated here so a new spelling is one clause rather than a fix
    spread across the detector:

    * the LATERAL / join wrapper carries an ``OUTER`` keyword (spark ``LATERAL VIEW OUTER
      explode(...)``, read from ``kernel.args['outer']``);
    * the flatten function is an outer variant whose row-preservation is in the node type or
      name (spark ``explode_outer`` -> ``_ExplodeOuter``, ``posexplode_outer`` ->
      ``PosexplodeOuter``);
    * the flatten call carries an ``OUTER => TRUE`` keyword argument (snowflake
      ``FLATTEN(... OUTER => TRUE)``).
    """
    if bool(kernel.args.get("outer")):
        return True
    fn = kernel.this if isinstance(kernel, _FLATTEN_WRAPPER_TYPED) else kernel
    return _is_outer_flatten_form(fn) or _has_outer_kwarg(fn)


def _is_outer_flatten_form(fn: Expr | None) -> bool:
    """True for the explode variants whose outer-ness is encoded in the function itself: a
    dedicated type (``_ExplodeOuter``, ``PosexplodeOuter``) or an anonymous ``*_outer`` name.
    Both spell the row-preserving form as a name ending in ``outer``."""
    if fn is None:
        return False
    name = (
        fn.this if isinstance(fn, exp.Anonymous) and isinstance(fn.this, str) else type(fn).__name__
    )
    return name.lower().endswith("outer")


def _has_outer_kwarg(fn: Expr) -> bool:
    """True if the flatten call carries ``OUTER => TRUE`` (snowflake ``FLATTEN``), parsed as
    a ``Kwarg`` named ``outer`` among the call's direct arguments."""
    for kw in (fn.this, *fn.args.get("expressions", [])):
        if (
            isinstance(kw, exp.Kwarg)
            and isinstance(kw.this, exp.Var)
            and kw.this.name.lower() == "outer"
        ):
            value = kw.expression
            return value.this if isinstance(value, exp.Boolean) else value is not None
    return False


def _unnest_arg_provably_nonempty(
    kernel: Expr,
    column_is_nonempty: Callable[[exp.Column], bool] | None,
    nullable_tables: set[str],
) -> bool:
    """True when every array ``kernel`` unnests is provably non-empty, so no parent row drops.

    Only the ``UNNEST`` spelling carries its array arguments where we can read them; the
    ``explode``/``flatten`` function forms wrap the array in dialect-specific ways and are
    left to the outer-form check. ``UNNEST(a, b)`` zips several arrays, every one of which
    must be non-empty for the row to survive."""
    if not isinstance(kernel, exp.Unnest):
        return False
    args = [a for a in kernel.expressions if isinstance(a, Expr)]
    if not args:
        return False
    return all(_array_expr_nonempty(a, column_is_nonempty, nullable_tables) for a in args)


def _array_expr_nonempty(
    arg: Expr,
    column_is_nonempty: Callable[[exp.Column], bool] | None,
    nullable_tables: set[str],
) -> bool:
    """Whether one unnested expression is provably non-empty.

    The intrinsic constructors the SQL vocabulary proves from the node alone are the always-on
    local cases: a literal ``ARRAY[...]``, a ``GENERATE_ARRAY`` over literal bounds. For a
    column, the answer comes from ``column_is_nonempty``, the lineage-grounded predicate the
    audit supplies; without it, a column is treated as opaque, so the detector module stays
    free of lineage types.

    Non-emptiness is proved where the array is *produced*. A column that arrives through the
    nullable side of an outer join can still be NULL at the unnest, and ``UNNEST(NULL)`` drops
    the row, so a column read from an outer-join-optional relation is never cleared here even
    when its value is non-empty wherever it exists."""
    if array_literal_nonempty(arg) or generator_provably_nonempty(arg):
        return True
    if column_is_nonempty is None or not isinstance(arg, exp.Column):
        return False
    if _read_through_nullable_join(arg, nullable_tables):
        return False
    return column_is_nonempty(arg)


def _read_through_nullable_join(col: exp.Column, nullable_tables: set[str]) -> bool:
    """Whether ``col`` is read from a relation an outer join can NULL-pad in its scope.

    A qualified column is unsafe exactly when its table is one of ``nullable_tables``. An
    unqualified column cannot be pinned to a preserved side once any relation in scope is
    nullable, so it is treated as unsafe too, which keeps the clear sound at the cost of a
    little precision."""
    if not nullable_tables:
        return False
    table = sg.column_table(col)
    return table is None or table in nullable_tables


def _inner_flatten_finding(node: Expr) -> Finding:
    return finding_at(
        FindingKind.INNER_FLATTEN_ROW_DROP,
        message=(
            f"inner array flatten {sg.render_sql(node)} drops the parent row when the array "
            "is empty or NULL, behaving like an inner join against the unnested set. If every "
            "parent row should survive, use the row-preserving outer form (LEFT JOIN "
            "UNNEST(...) ON TRUE, FLATTEN(... OUTER => TRUE), LATERAL VIEW OUTER explode(...))."
        ),
        node=node,
    )


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
    return sg.matches_typed_or_named(node, tuple(_NON_DETERMINISTIC_TYPED), names)


def _non_deterministic_name(call: Expr) -> str:
    if isinstance(call, exp.Anonymous) and isinstance(call.this, str):
        return f"{call.this}()"
    return type(call).__name__
