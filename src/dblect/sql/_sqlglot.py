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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

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
    """The ``FROM`` clause of a ``SELECT``, or ``None`` if absent.

    sqlglot 30+ keys the arg ``"from_"``; older 25.x kept it as ``"from"``.
    We try both so the static-analysis layer doesn't pin a minor version.
    """
    return cast("exp.From | None", sel.args.get("from_") or sel.args.get("from"))


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


def group_targets(sel: exp.Select) -> tuple[Expr, ...]:
    """The expressions ``sel`` groups by, with positional ordinals and output-alias references
    resolved to the projections they name.

    ``GROUP BY 1`` and ``GROUP BY revenue_day`` both name a projection, so a reader walking the
    ``Group`` node's arguments finds an ``exp.Literal`` or a table-less ``exp.Column`` where the
    semantics are the projected expression. Every structural check over grouping keys wants that
    expression, so the resolution belongs here rather than in each caller. We resolve by lookup
    instead of rewriting the tree, so the caller's nodes keep the source positions that findings
    report line numbers from.

    Every adapter dblect targets reads a bare integer in GROUP BY as a position, so this needs
    no dialect gate.

    A target we cannot resolve is returned unchanged, leaving callers to treat it as the opaque
    target it is. :func:`_resolve_ordinal` and :func:`_resolve_name` each document when they
    decline.
    """
    group = group_of(sel)
    if group is None:
        return ()
    projections = cast("list[Expr]", sel.expressions)
    projected = _projection_expressions_by_output_name(sel)
    return tuple(
        _resolve_group_target(target, sel, projections, projected) for target in group.expressions
    )


def _resolve_group_target(
    target: Expr, sel: exp.Select, projections: Sequence[Expr], projected: Mapping[str, Expr]
) -> Expr:
    resolved = _resolve_ordinal(target, projections)
    return resolved if resolved is not None else _resolve_name(target, sel, projected)


def _resolve_name(target: Expr, sel: exp.Select, projected: Mapping[str, Expr]) -> Expr:
    """``target`` resolved to the projection it names, if it is an output-name reference.

    Only an unqualified column name can name a projection; ``t.k`` binds to the table.

    A name matching a projection that is *itself* the bare column of that name resolves
    unconditionally: SQL would bind the name to that input column, and the projection is that
    same column, so the two readings agree and the shadowing question does not arise. This is
    the ``select orders.customer_id ... group by customer_id`` idiom. Any other projection is
    a renaming, where an input column of the same name would win over the output name, so it
    resolves only when :func:`_shadows_an_input_column` finds no such column.
    """
    if not isinstance(target, exp.Column) or target.args.get("table") is not None:
        return target
    if not isinstance(target.this, exp.Identifier):
        return target
    name = column_name(target)
    projection = projected.get(name)
    if projection is None:
        return target
    if isinstance(projection, exp.Column) and column_name(projection) == name:
        return projection
    return target if _shadows_an_input_column(name, sel) else projection


def _projection_expressions_by_output_name(sel: exp.Select) -> dict[str, Expr]:
    """Each output name in ``sel``'s projection mapped to the expression behind it.

    An output name carried by two projections names neither unambiguously, so it is dropped
    rather than resolved to whichever came last. Such a query does not run anyway: an engine
    binds the first and then rejects the second as ungrouped.
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


def _shadows_an_input_column(name: str, sel: exp.Select) -> bool:
    """Whether ``name`` may bind to an input column of ``sel`` rather than to its output alias.

    SQL resolves a GROUP BY name against the input columns first and only then against the
    output aliases, so ``select b.amt * 2 as amt ... group by amt`` groups by ``b.amt``. Naming
    the input columns exactly needs a schema, which the AST layer does not have, so we treat any
    column of that name referenced elsewhere in the query as evidence the name is taken and
    decline to resolve. That is conservative in the direction of the behaviour before aliases
    resolved at all: an input column the query never mentions still slips through, which is the
    known-permissive edge of this rule.

    ``GROUP BY`` and ``ORDER BY`` are excluded from the sweep because both resolve names against
    output aliases themselves, so a reference there is not evidence of an input column.

    A projection that expands to unknown width settles the question the other way: ``select
    a.*`` carries every column of ``a`` without naming one, so any name may be among them and
    the sweep has nothing to find. Treating that as "no input column" would resolve the alias
    and report against an expression the engine never grouped by, turning the permissive edge
    above into a false positive, so an unexpanded star declines every name in the query.
    """
    if any(_expands_to_unknown_width(p) for p in sel.expressions):
        return True
    for key, arg in sel.args.items():
        if key in ("group", "order"):
            continue
        nodes = cast("list[object]", arg) if isinstance(arg, list) else [cast("object", arg)]
        for node in nodes:
            if isinstance(node, Expr) and any(column_name(c) == name for c in find_columns(node)):
                return True
    return False


@dataclass(frozen=True)
class OrderTarget:
    """One statement-level ``ORDER BY`` target, and which namespace its expression is in.

    A positional target resolves to the named projection's own expression, so it arrives in the
    query's *source* namespace, already past the ``AS`` binding. A target spelled as a name is
    in the *output* namespace, where a caller matching against source columns still has to
    translate it through the projection's aliases.

    Conflating the two re-translates a resolved source column whenever some *other* projection
    is aliased to that name: in ``select id as x, other as id ... order by 1`` the ordinal
    resolves to ``id``, which a second pass through the alias map would turn into ``other``.
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
    if resolved is None:
        return OrderTarget(target, in_source_namespace=False)
    return OrderTarget(resolved, in_source_namespace=True)


def imposes_row_order(order: exp.Order | None) -> bool:
    """Whether ``order`` actually pins the order of the rows it governs.

    An ORDER BY inside a window or an aggregate takes expressions, never the positional
    references a statement-level ORDER BY accepts, so a literal there is a constant: every row
    sorts equal and the ranking falls back to whatever physical order the engine happened to
    have. An ordering whose targets reference no column therefore pins nothing, and the caller's
    "no ORDER BY" hazard applies to it unchanged.

    A target referencing a column counts even when the column sits inside a subquery, where the
    value is constant per row and so orders nothing either. That keeps the answer conservative:
    the caller stays silent rather than reporting a hazard this rule cannot yet prove.
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
    """``node`` as a ``ROW_NUMBER() OVER (...)`` window, or ``None``. Only ``ROW_NUMBER`` grounds
    a dedup key: it ranks distinctly within a partition, so ``= 1`` keeps exactly one row, whereas
    ``RANK`` / ``DENSE_RANK`` share a rank across ties and can keep several."""
    if isinstance(node, exp.Window) and isinstance(node.this, exp.RowNumber):
        return node
    return None


def _is_literal_one(node: Expr) -> bool:
    return isinstance(node, exp.Literal) and not node.args.get("is_string") and node.this == "1"


def rank_one_guard_operand(leaf: Expr) -> Expr | None:
    """The operand a ``= 1`` / ``<= 1`` dedup guard constrains to the top rank, or ``None``.
    Recognises ``X = 1``, ``1 = X``, ``X <= 1`` and ``1 >= X`` with a literal integer ``1`` (a
    row number is ``>= 1``, so ``<= 1`` coincides with ``= 1``). Any other comparison keeps more
    than the top row and grounds no key. ``X`` is returned unevaluated for the caller to test."""
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

    sqlglot stamps token-position metadata onto the leaves it builds from a single token,
    which is mostly ``Identifier`` but also ``Literal``, so we walk every descendant and take
    min/max over the ``meta["line"]`` values we find. Reading identifiers alone left the
    literal-only expressions with no span at all, which is how ``GROUP BY 1`` reported a
    finding against line 0. Returns ``None`` when no descendant carries a usable line number.

    Line numbers refer to the SQL the parser saw (the model's ``compiled_code``).
    """
    lines: list[int] = []
    for node in e.walk():
        line = node.meta.get("line") if node.meta else None
        if isinstance(line, int):
            lines.append(line)
    if not lines:
        return None
    return min(lines), max(lines)
