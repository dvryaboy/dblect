"""Typed accessors over sqlglot's AST.

sqlglot's `Expr.args` is typed as ``dict[str, Any]`` and several attributes on
expression nodes (``side``, ``kind``, ``alias_or_name``) are not narrowed in
the upstream stubs. We pay the conversion cost once here so the rest of
``dblect.sql`` reads as if sqlglot were strictly typed.

The casts are safe by construction: ``Select.args["joins"]`` is always a
``list[Join]`` when present, ``Select.args["from_"]`` is always a ``From``,
and so on (the keys are sqlglot's own naming conventions). Each helper
documents its key and what shape it returns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeGuard, TypeVar, cast

import sqlglot.expressions as exp
from sqlglot import Expr


class JoinSide(StrEnum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"
    CROSS = "cross"
    SEMI = "semi"
    ANTI = "anti"


def from_of(sel: exp.Select) -> exp.From | None:
    """The ``FROM`` clause of a ``SELECT``, or ``None`` if absent."""
    return cast("exp.From | None", sel.args.get("from_"))


def where_of(sel: exp.Select) -> exp.Where | None:
    return cast("exp.Where | None", sel.args.get("where"))


def qualify_of(sel: exp.Select) -> exp.Qualify | None:
    return cast("exp.Qualify | None", sel.args.get("qualify"))


def joins_of(sel: exp.Select) -> list[exp.Join]:
    return cast("list[exp.Join]", sel.args.get("joins") or [])


def laterals_of(sel: exp.Select) -> list[exp.Lateral]:
    """The standalone laterals stored on ``sel`` (spark ``LATERAL VIEW``), distinct from the
    join-arm laterals that live among :func:`joins_of`."""
    return cast("list[exp.Lateral]", sel.args.get("laterals") or [])


def group_of(sel: exp.Select) -> exp.Group | None:
    return cast("exp.Group | None", sel.args.get("group"))


_V = TypeVar("_V")


def with_scope(
    node: Expr, cte_scope: Mapping[str, _V], resolve: Callable[[Expr, Mapping[str, _V]], _V]
) -> dict[str, _V]:
    """``cte_scope`` extended with the CTEs ``node`` declares, each resolved by
    ``resolve`` in the scope the ones before it built. The one CTE fold every
    relation walk shares, whatever value it carries per scope."""
    local = dict(cte_scope)
    with_ = node.args.get("with_")
    if isinstance(with_, exp.With):
        for cte in with_.expressions:
            if isinstance(cte, exp.CTE) and isinstance(cte.this, Expr):
                local[cte.alias_or_name] = resolve(cte.this, local)
    return local


def union_arms(u: exp.Union) -> list[Expr] | None:
    """The arms of a union chain in order: same-operator nesting flattened and
    parenthesized arms unwrapped, since parens change neither the merged rows nor
    the arms' output names (dbt_utils-style generated unions parenthesize every
    arm). Mixing UNION and UNION ALL is fine: the merged rows are drawn from the
    arms' rows either way. ``None`` when any link merges by name (``BY NAME``,
    ``CORRESPONDING``), where alignment is by alias rather than position."""
    if u.args.get("by_name"):
        return None
    arms: list[Expr] = []
    for side in (u.this, u.expression):
        if not isinstance(side, Expr):
            return None
        node = side.unnest()
        if isinstance(node, exp.Union):
            inner = union_arms(node)
            if inner is None:
                return None
            arms.extend(inner)
        else:
            arms.append(node)
    return arms


class GroupBinding(StrEnum):
    """How firmly a ``GROUP BY`` target is bound to the expression it denotes.

    ``WRITTEN`` is a target spelled out in full, or one we declined to resolve, so the node is
    its own meaning. ``DECIDED`` is fixed by SQL's own rules: an ordinal, or a name whose
    projection is that very column. ``PRESUMED`` is a renaming alias, where SQL
    would bind an input column of that name first and the AST cannot rule one out.

    A detector may read all three, since over-reporting is its safe direction. A consumer that
    treats the group key as an established fact stops at ``DECIDED`` (see
    :attr:`GroupTarget.grounded_expression`): keys and dependencies are read downstream to
    clear hazards, so a wrong resolution there silences real findings instead of adding a
    spurious one.
    """

    WRITTEN = "written"
    DECIDED = "decided"
    PRESUMED = "presumed"


@dataclass(frozen=True)
class GroupTarget:
    """One ``GROUP BY`` target: the expression it denotes, the node the query writes, and how
    firmly the two are tied together.

    A finding about the *grouping decision* belongs at ``written_at``, so ``GROUP BY 1`` is
    diagnosed at the clause an analyst reads. One about something written inside the target (a
    ``now()`` call) belongs at ``expression``, where that call is spelled out. The two nodes
    coincide for a target written in full. ``binding`` decides whether a consumer may treat
    ``expression`` as an established fact: see :attr:`grounded_expression`.
    """

    expression: Expr
    written_at: Expr
    binding: GroupBinding

    @property
    def grounded_expression(self) -> Expr:
        """The reading a consumer may treat as an established fact: the resolved expression
        where SQL's own rules decide the binding, and the written node where they do not.

        Falling back to ``written_at`` on a ``PRESUMED`` binding is the conservative reading, and
        it is the one SQL takes whenever the name really is an input column. Two targets can also
        resolve onto one node (``SELECT x AS a ... GROUP BY a, x`` where ``a`` is an input
        column), which would shrink the group key, and a shorter key is the stronger claim.
        """
        return self.written_at if self.binding is GroupBinding.PRESUMED else self.expression


def group_targets(sel: exp.Select) -> tuple[GroupTarget, ...]:
    """The targets ``sel`` groups by, with ordinals and output-name references resolved to the
    projections they name.

    ``GROUP BY 1`` and ``GROUP BY revenue_day`` both name a projection, so a reader walking the
    ``Group`` node's arguments finds a literal or a table-less column where the semantics are the
    projected expression. Every structural check over grouping keys wants that expression, so the
    resolution belongs here rather than in each caller. Resolution is by lookup rather than by
    rewriting the tree, so the nodes keep their source positions.

    Every adapter dblect targets reads a bare integer in GROUP BY as a position, so no dialect
    gate is needed. A target we cannot resolve carries itself as its own ``expression``.
    """
    group = group_of(sel)
    if group is None:
        return ()
    projections = cast("list[Expr]", sel.expressions)
    projected = _projection_expressions_by_output_name(sel)
    return tuple(
        _resolve_group_target(target, projections, projected) for target in group.expressions
    )


def _resolve_group_target(
    target: Expr, projections: Sequence[Expr], projected: Mapping[str, Expr]
) -> GroupTarget:
    resolved = _resolve_ordinal(target, projections)
    if resolved is not None:
        return GroupTarget(resolved, target, GroupBinding.DECIDED)
    expression, binding = _resolve_name(target, projected)
    return GroupTarget(expression, target, binding)


def _names_an_output_column(e: Expr) -> TypeGuard[exp.Column]:
    """Whether ``e`` is the spelling that can bind to a projection's output name.

    Only an unqualified column reference can. A qualified ``t.k`` binds to that relation's column
    and never picks up a matching output name, in a GROUP BY and a statement-level ORDER BY alike
    (duckdb: ``select id as x, other as id ... order by orders.id`` sorts by ``id``, while
    ``order by id`` sorts by ``other``).
    """
    return isinstance(e, exp.Column) and isinstance(e.this, exp.Identifier) and e.table == ""


def _resolve_name(target: Expr, projected: Mapping[str, Expr]) -> tuple[Expr, GroupBinding]:
    """``target`` resolved to the projection it names, if it is an output-name reference, with
    how firmly the two are tied together.

    A name whose projection is *itself* the bare column of that name is ``DECIDED``: SQL binds
    the name to that input column, the projection is that same column, so the two readings agree.
    This is the ``select orders.customer_id ... group by customer_id`` idiom.

    Any other projection is a renaming, and SQL binds a GROUP BY name to an input column before
    an output alias, so ``select b.amt * 2 as amt ... group by amt`` really groups by ``b.amt``
    while this resolves it to ``b.amt * 2``. Ruling that out needs a schema the AST layer does not
    have, hence ``PRESUMED``. A reader reasoning structurally lands on the same join side either
    way, which is why the detectors accept it: their error direction is to over-report.
    """
    if not _names_an_output_column(target):
        return target, GroupBinding.WRITTEN
    name = column_name(target)
    projection = projected.get(name)
    if projection is None:
        return target, GroupBinding.WRITTEN
    if isinstance(projection, exp.Column) and column_name(projection) == name:
        return projection, GroupBinding.DECIDED
    return projection, GroupBinding.PRESUMED


def _projection_expressions_by_output_name(sel: exp.Select) -> dict[str, Expr]:
    """Each output name in ``sel``'s projection mapped to the expression behind it.

    A name carried by two projections names neither unambiguously, so it is dropped rather than
    resolved to whichever came last. Such a query does not run anyway: an engine binds the first
    and rejects the second as ungrouped.
    """
    out: dict[str, Expr] = {}
    duplicated: set[str] = set()
    for proj in sel.expressions:
        if isinstance(proj, exp.Alias):
            name, expression = proj.alias_or_name, cast("Expr", proj.this)
        elif isinstance(proj, exp.Column) and isinstance(proj.this, exp.Identifier):
            name, expression = column_name(proj), proj
        else:
            continue
        if name in out:
            duplicated.add(name)
        out[name] = expression
    return {name: e for name, e in out.items() if name not in duplicated}


@dataclass(frozen=True)
class OrderTarget:
    """One statement-level ``ORDER BY`` target, and which namespace its expression is in.

    A resolved ordinal and a qualified ``t.k`` both name a source column outright, already past
    the ``AS`` binding. Only a bare name is in the *output* namespace, where a caller matching
    against source columns has to translate it through the projection's aliases first.

    Conflating the two re-translates a source column whenever some *other* projection is aliased
    to that name: in ``select id as x, other as id ... order by 1`` the target is ``id``, which a
    second pass through the alias map would turn into ``other``. A target naming no single column
    (``order by lower(k)``) carries the source reading by default, which nothing acts on: the one
    consumer declines a non-bare-column target first.
    """

    expression: Expr
    in_source_namespace: bool


def statement_order_targets(sel: exp.Select) -> tuple[OrderTarget, ...]:
    """The statement-level ``ORDER BY`` targets of ``sel``, with ordinals resolved and each
    target's ``exp.Ordered`` wrapper removed.

    Unlike the ORDER BY inside a window or an aggregate, where a literal is a constant that
    orders nothing (see :func:`imposes_row_order`), a statement-level ``ORDER BY 1`` is a
    positional reference to the first projection.
    """
    order = cast("exp.Order | None", sel.args.get("order"))
    if order is None:
        return ()
    projections = cast("list[Expr]", sel.expressions)
    targets = (t.this if isinstance(t, exp.Ordered) else t for t in order.expressions)
    return tuple(_order_target(t, projections) for t in targets)


def _order_target(target: Expr, projections: Sequence[Expr]) -> OrderTarget:
    resolved = _resolve_ordinal(target, projections)
    if resolved is not None:
        return OrderTarget(resolved, in_source_namespace=True)
    return OrderTarget(target, in_source_namespace=not _names_an_output_column(target))


def imposes_row_order(order: exp.Order | None) -> bool:
    """Whether ``order`` actually pins the order of the rows it governs.

    An ORDER BY inside a window or an aggregate takes expressions, never the positional
    references a statement-level ORDER BY accepts, so a literal there is a constant: every row
    sorts equal and the ranking follows whatever physical order the engine had. An ordering whose
    targets reference no column therefore pins nothing.

    A column inside a subquery counts even though it is constant per row and orders nothing
    either, which keeps the answer conservative: the caller stays silent rather than report a
    hazard this rule cannot prove.
    """
    if order is None or not order.expressions:
        return False
    return any(find_columns(e) for e in order.expressions)


def _resolve_ordinal(target: Expr, projections: Sequence[Expr]) -> Expr | None:
    """The projection ``target`` names positionally, or ``None`` when it names none.

    Only a bare positive integer literal is positional. A string (``GROUP BY 'x'``), a float,
    and a negation (which parses as ``Neg`` over the literal, not a literal) are grouped
    values, so they name no position. Nor does an index past the end of the projection list,
    or one reaching over a ``SELECT *`` that expands to an unknown number of columns, so
    position N is not the Nth listed projection.
    """
    if not (isinstance(target, exp.Literal) and target.is_int):
        return None
    index = int(target.this)
    if not 1 <= index <= len(projections):
        return None
    prefix = projections[:index]
    if any(_expands_to_unknown_width(p) for p in prefix):
        return None
    return prefix[-1].unalias()


def _expands_to_unknown_width(projection: Expr) -> bool:
    """Whether ``projection`` stands for an unknown number of output columns.

    A bare ``*`` or a qualified ``t.*`` does; ``count(*)`` does not, so this looks at the
    projection itself rather than searching it for a ``Star``.
    """
    return isinstance(projection, exp.Star) or (
        isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
    )


def on_of(j: exp.Join) -> Expr | None:
    return cast("Expr | None", j.args.get("on"))


def order_of(w: exp.Window) -> exp.Order | None:
    return cast("exp.Order | None", w.args.get("order"))


# Modifiers that sqlglot stacks above an aggregate's ORDER BY clause. The order
# can sit directly at the aggregate's `this`, or under a top-n LIMIT, or below a
# DISTINCT (sqlglot has produced both `Limit -> Order` and `Order -> Distinct`),
# so we walk transparently through these to reach the ordering itself.
_AGGREGATE_CLAUSE_MODIFIERS: tuple[type[Expr], ...] = (exp.Limit, exp.Distinct)


def aggregate_order_of(agg: Expr) -> exp.Order | None:
    """The ORDER BY governing an aggregate's element order, if any.

    Returns the aggregate's own ``Order`` clause, seeing through the LIMIT and
    DISTINCT modifiers sqlglot wraps around it (``ARRAY_AGG(x ORDER BY y LIMIT n)``
    parses as ``Limit -> Order``). Returns ``None`` when the aggregate has no
    ordering of its own. An ``Order`` nested inside a subquery or other argument
    expression is not the aggregate's ordering and is deliberately not returned.
    """
    inner = agg.this
    while isinstance(inner, _AGGREGATE_CLAUSE_MODIFIERS):
        inner = inner.this
    return inner if isinstance(inner, exp.Order) else None


def aggregate_limit_of(agg: Expr) -> exp.Limit | None:
    """The top-n ``LIMIT`` modifier on an aggregate, if present.

    ``ARRAY_AGG(x ORDER BY y LIMIT n)`` (the top-n idiom) keeps only the first ``n``
    elements; the ``Limit`` sits among the modifiers stacked above the ORDER BY, so
    walk through them the same way :func:`aggregate_order_of` does. Returns ``None``
    when the aggregate folds every element (no inner ``LIMIT``).
    """
    inner = agg.this
    while isinstance(inner, _AGGREGATE_CLAUSE_MODIFIERS):
        if isinstance(inner, exp.Limit):
            return inner
        inner = inner.this
    return None


def limit_keeps_no_rows(limit: exp.Limit) -> bool:
    """True when ``limit`` provably keeps zero rows (its count is the literal ``0``).

    ``LIMIT 0`` yields the empty set whatever the row order, so it is deterministic by
    construction: there is no slice to pick and no tie to break. This is the schema-only
    stub idiom (``select cast(null as ...) ... limit 0``, an empty table with a fixed
    shape) and the empty-array aggregate (``array_agg(x ... limit 0)``). The check is
    deliberately narrow: only a literal ``0`` count is provably empty, so a parameter or
    an expression that might evaluate to ``0`` is not exempted and the caller keeps its
    conservative posture.
    """
    count = limit.expression
    return isinstance(count, exp.Literal) and not count.args.get("is_string") and count.this == "0"


def partition_of(w: exp.Window) -> list[Expr]:
    return cast("list[Expr]", w.args.get("partition_by") or [])


def fn_of(w: exp.Window) -> Expr | None:
    return cast("Expr | None", w.this)


def row_number_window(node: Expr) -> exp.Window | None:
    """``node`` as a ``ROW_NUMBER() OVER (...)`` window, or ``None``. Only ``ROW_NUMBER`` gives a
    working dedup key: it ranks distinctly within a partition, so ``= 1`` keeps exactly one row,
    whereas ``RANK`` / ``DENSE_RANK`` share a rank across ties and can keep several."""
    if isinstance(node, exp.Window) and isinstance(node.this, exp.RowNumber):
        return node
    return None


def _is_literal_one(node: Expr) -> bool:
    return isinstance(node, exp.Literal) and not node.args.get("is_string") and node.this == "1"


def rank_one_guard_operand(leaf: Expr) -> Expr | None:
    """The operand a ``= 1`` / ``<= 1`` dedup guard constrains to the top rank, or ``None``.
    Recognises ``X = 1``, ``1 = X``, ``X <= 1`` and ``1 >= X`` with a literal integer ``1`` (a
    row number is ``>= 1``, so ``<= 1`` coincides with ``= 1``). Any other comparison keeps more
    than the top row and yields no usable dedup key. ``X`` is returned unevaluated for the
    caller to test."""
    if isinstance(leaf, exp.EQ):
        if _is_literal_one(leaf.expression):
            return leaf.this
        if _is_literal_one(leaf.this):
            return leaf.expression
    elif isinstance(leaf, exp.LTE) and _is_literal_one(leaf.expression):
        return leaf.this  # X <= 1
    elif isinstance(leaf, exp.GTE) and _is_literal_one(leaf.this):
        return leaf.expression  # 1 >= X
    return None


def window_output_alias(w: exp.Window) -> str | None:
    """The SELECT-list alias a window is projected under (``rn`` in ``row_number() ... as rn``).

    ``None`` when the window is not directly aliased (an unaliased projection, or one where the
    window is nested inside a larger expression that carries the alias). Callers use this to
    recognise a later reference to the window by name, e.g. a ``qualify rn = 1`` that names the
    rank rather than inlining the window.
    """
    parent = w.parent
    return parent.alias if isinstance(parent, exp.Alias) else None


def join_side_of(j: exp.Join) -> JoinSide:
    """The side of `j` as a `JoinSide`.

    Reads sqlglot's ``side`` and ``kind`` token strings. ``CROSS``/``SEMI``/``ANTI``
    live on ``kind`` and override side (sqlglot still records ``LEFT`` as the side of a
    ``LEFT SEMI JOIN``, but a semi join filters rather than pads, so it is not an outer
    join). A missing/empty side defaults to ``INNER``, the SQL default for ``a JOIN b``
    without a qualifier.
    """
    side = (j.side or "").upper()
    kind = (j.kind or "").upper()
    if "CROSS" in kind:
        return JoinSide.CROSS
    if "SEMI" in kind:
        return JoinSide.SEMI
    if "ANTI" in kind:
        return JoinSide.ANTI
    match side:
        case "LEFT":
            return JoinSide.LEFT
        case "RIGHT":
            return JoinSide.RIGHT
        case "FULL":
            return JoinSide.FULL
        case _:
            return JoinSide.INNER


def name_of(e: Expr) -> str:
    """``alias_or_name`` is the alias when there is one, the table/column name otherwise."""
    return e.alias_or_name


def outer_join_optional_aliases(sel: exp.Select) -> set[str]:
    """The aliases an outer join in ``sel`` leaves NULL-padded: its non-preserved sides.

    A LEFT join makes its right side optional, a RIGHT join its accumulated left, a FULL
    join both. Inner, cross, semi, and anti joins do not NULL-pad, so they contribute
    nothing. The aliases are returned by ``alias_or_name`` to line up with callers that
    qualify columns by the same alias. An alias absent from this set is on a preserved
    side: its rows survive the join un-padded.
    """
    from_ = from_of(sel)
    if from_ is None:
        return set()
    optional: set[str] = set()
    accumulated_left: set[str] = {name_of(from_.this)} if from_.this is not None else set()
    for j in joins_of(sel):
        right_name = name_of(j.this)
        side = join_side_of(j)
        if side is JoinSide.LEFT:
            optional.add(right_name)
        elif side is JoinSide.RIGHT:
            optional.update(accumulated_left)
        elif side is JoinSide.FULL:
            optional.add(right_name)
            optional.update(accumulated_left)
        accumulated_left.add(right_name)
    return optional


def joins_with_outer_dropped_aliases(
    sel: exp.Select,
) -> list[tuple[exp.Join, JoinSide, frozenset[str]]]:
    """Each join in ``sel`` with its side and the aliases whose unmatched rows it drops.

    A LEFT join drops its unmatched right rows; a RIGHT join its unmatched left rows (every
    alias accumulated to its left). A FULL join drops nothing, since both sides survive
    NULL-padded, and inner, cross, semi, and anti joins report an empty set (an inner join's
    unmatched rows belong to no single side, and semi/anti filter rather than pad). The
    accumulated-left context grows left to right, so a later RIGHT join sees the earlier
    tables.

    This is the per-join view a caller gates on when it cares about one join's own dropped
    side. It differs from :func:`outer_join_optional_aliases`, the output-nullable union that
    counts both sides of a FULL join (both can be NULL in the result) and is not scoped to a
    single join.
    """
    from_ = from_of(sel)
    out: list[tuple[exp.Join, JoinSide, frozenset[str]]] = []
    if from_ is None:
        return out
    accumulated_left: set[str] = {name_of(from_.this)} if from_.this is not None else set()
    for j in joins_of(sel):
        right_name = name_of(j.this)
        side = join_side_of(j)
        dropped: frozenset[str]
        if side is JoinSide.LEFT:
            dropped = frozenset({right_name})
        elif side is JoinSide.RIGHT:
            dropped = frozenset(accumulated_left)
        else:
            dropped = frozenset()
        out.append((j, side, dropped))
        accumulated_left.add(right_name)
    return out


def column_table(c: exp.Column) -> str | None:
    """The qualifier on a column reference (``a`` in ``a.id``), or ``None``."""
    return c.table or None


def column_name(c: exp.Column) -> str:
    return c.name


def column_key(c: exp.Column) -> tuple[str | None, str]:
    """The ``(qualifier, name)`` identity of a column reference, for matching columns by name.

    Two references share a key when they name the same column, ``a.id`` distinct from a bare
    ``id`` (an under-qualified reference matches conservatively, never spuriously).
    """
    return (column_table(c), column_name(c))


def find_columns(e: Expr) -> list[exp.Column]:
    return list(e.find_all(exp.Column))


def find_all_selects(e: Expr) -> list[exp.Select]:
    return list(e.find_all(exp.Select))


def find_all_coalesce(e: Expr) -> list[exp.Coalesce]:
    return list(e.find_all(exp.Coalesce))


def find_all_windows(e: Expr) -> list[exp.Window]:
    return list(e.find_all(exp.Window))


def find_all_aggfunc(e: Expr) -> list[Expr]:
    return cast("list[Expr]", list(e.find_all(exp.AggFunc)))


# The order-sensitive aggregates: their element order is part of the result, so an absent or
# non-total ORDER BY makes the output non-deterministic. ``GroupConcat`` is sqlglot's node for
# both ``GROUP_CONCAT`` and ``STRING_AGG``.
ORDERED_AGGREGATE_FUNCTIONS: tuple[type[Expr], ...] = (exp.ArrayAgg, exp.GroupConcat)


def find_all_ordered_aggregates(e: Expr) -> list[Expr]:
    return list(e.find_all(*ORDERED_AGGREGATE_FUNCTIONS))


def render_sql(e: Expr) -> str:
    return e.sql()


def matches_typed_or_named(
    node: Expr, typed: tuple[type[Expr], ...], names: frozenset[str]
) -> bool:
    """True if ``node`` is one of ``typed`` (``isinstance``, so subclasses look through), or
    a function sqlglot left as ``exp.Anonymous`` whose name (case-insensitive) is in
    ``names``. The dialect parsers pick a dedicated type for most constructs; the few a
    dialect leaves anonymous are matched by name. Every entry in ``names`` must be lowercase.
    """
    if isinstance(node, typed):
        return True
    return (
        isinstance(node, exp.Anonymous)
        and isinstance(node.this, str)
        and node.this.lower() in names
    )


def equality_cols_on_alias(predicate: Expr, alias: str) -> frozenset[str] | None:
    """Columns on `alias` appearing in conjunctive equalities in `predicate`.

    Walks the AND-conjunction of `predicate`; for each leaf, accepts only
    ``exp.EQ`` between two bare columns where exactly one column's qualifier
    equals `alias`. Returns the set of column names on the `alias` side.

    Returns ``None`` if `predicate` contains anything other than a conjunction
    of such equalities (a disjunction, a function call, a range comparison,
    or an equality whose alias mix is ambiguous). Callers treat ``None`` as
    "can't simplify to a clean join-key" and conservatively skip.
    """
    cols: set[str] = set()
    for leaf in conjunctive_leaves(predicate):
        if not isinstance(leaf, exp.EQ):
            return None
        left = leaf.this
        right = leaf.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            return None
        left_alias = column_table(left)
        right_alias = column_table(right)
        on_alias = [c for c, t in ((left, left_alias), (right, right_alias)) if t == alias]
        off_alias = [c for c, t in ((left, left_alias), (right, right_alias)) if t != alias]
        if len(on_alias) != 1 or len(off_alias) != 1:
            return None
        cols.add(column_name(on_alias[0]))
    return frozenset(cols)


def equality_cols_by_alias(predicate: Expr) -> dict[str, frozenset[str]] | None:
    """Per-alias join-key columns from a conjunction of column equalities, in one walk.

    The multi-alias companion to :func:`equality_cols_on_alias`: it flattens the conjunction
    once and returns every mentioned alias mapped to its key columns, so a caller reasoning
    about all of a join's sides does not re-walk the predicate per alias. Returns ``None``
    with the same meaning as the single-alias form, when ``predicate`` is anything other than
    a conjunction of bare column-to-column equalities (the caller then skips the whole join).
    An alias maps to its columns only when it appears exactly once in every conjunct, the rule
    :func:`equality_cols_on_alias` enforces; aliases that fail it are simply absent.
    """
    leaves = conjunctive_leaves(predicate)
    sides: list[tuple[tuple[str | None, str], tuple[str | None, str]]] = []
    for leaf in leaves:
        if not isinstance(leaf, exp.EQ):
            return None
        left, right = leaf.this, leaf.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            return None
        sides.append((column_key(left), column_key(right)))
    out: dict[str, frozenset[str]] = {}
    for alias in {a for pair in sides for a, _ in pair if a is not None}:
        cols: set[str] = set()
        for left_side, right_side in sides:
            on_alias = [c for a, c in (left_side, right_side) if a == alias]
            if len(on_alias) != 1:
                break
            cols.add(on_alias[0])
        else:
            out[alias] = frozenset(cols)
    return out


def equality_literal_columns(predicate: Expr) -> tuple[exp.Column, ...]:
    """Columns a conjunct of `predicate` pins to a literal (``col = 'usd'``).

    Walks the AND-conjunction; a leaf contributes its column only when it is an
    ``exp.EQ`` between a bare column and a literal, in either order. Other leaves
    are simply skipped (unlike :func:`equality_cols_on_alias`, a non-equality
    conjunct does not poison the rest: each pin stands on its own conjunct).
    """
    out: list[exp.Column] = []
    for leaf in conjunctive_leaves(predicate):
        if not isinstance(leaf, exp.EQ):
            continue
        sides = (leaf.this, leaf.expression)
        for col, lit in (sides, sides[::-1]):
            if (
                isinstance(col, exp.Column)
                and not isinstance(col.this, exp.Star)
                and isinstance(lit, exp.Literal)
            ):
                out.append(col)
                break
    return tuple(out)


def equality_column_pairs(predicate: Expr) -> tuple[tuple[exp.Column, exp.Column], ...]:
    """Column-to-column equalities in `predicate` (``a.x = b.y``), as ordered pairs.

    Walks the AND-conjunction; a leaf contributes a pair only when it is an ``exp.EQ``
    between two bare columns. This is the join-key extraction a join ON predicate needs
    (each equated pair, both sides resolved), companion to :func:`equality_literal_columns`
    for the literal-pin case. Non-equality and non-column leaves are skipped, each pair
    standing on its own conjunct."""
    out: list[tuple[exp.Column, exp.Column]] = []
    for leaf in conjunctive_leaves(predicate):
        if not isinstance(leaf, exp.EQ):
            continue
        left, right = leaf.this, leaf.expression
        if (
            isinstance(left, exp.Column)
            and isinstance(right, exp.Column)
            and not isinstance(left.this, exp.Star)
            and not isinstance(right.this, exp.Star)
        ):
            out.append((left, right))
    return tuple(out)


def conjunctive_leaves(predicate: Expr) -> list[Expr]:
    """Flatten an ``AND``-only conjunction into its leaves; non-AND nodes are leaves."""
    if isinstance(predicate, exp.And):
        return [*conjunctive_leaves(predicate.this), *conjunctive_leaves(predicate.expression)]
    return [predicate]


def line_range(e: Expr) -> tuple[int, int] | None:
    """The 1-indexed (start, end) source-line span covered by `e`.

    sqlglot stamps ``meta["line"]`` at the token a node opens on, and not only on identifiers:
    literals, function calls, and stars carry one too. Every stamp lies inside its own node's
    span, so walking all descendants and taking min/max only tightens the answer. Reading
    identifiers alone left literal-only expressions with no span, which is how ``GROUP BY 1``
    reported against line 0. Returns ``None`` when no descendant carries a line number.

    Line numbers refer to the SQL the parser saw (the model's ``compiled_code``).
    """
    lines: list[int] = []
    for node in e.walk():
        line = node.meta.get("line")
        if isinstance(line, int):
            lines.append(line)
    if not lines:
        return None
    return min(lines), max(lines)
