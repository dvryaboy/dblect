"""The shared anti-join classifier.

An anti-join keeps a probe-side row only when it has no matching row on the other side, so
it removes rows and never multiplies them. dblect writes that one operator four ways, and
:func:`anti_joins_of` reads each back to the same shape: the probe relation ``L``, the
matched relation ``R``, and the predicate columns the two are compared on.

The uniqueness and fan-out reducers key off an anti-join's *join arm* (a native ``ANTI JOIN``
or the ``LEFT JOIN ... IS NULL`` idiom): a filter over the probe side preserves its keys and
cannot fan out. The nullability detector consumes every form, since a NULL probe key is kept
as a spurious non-match rather than dropped, the inverse of an ordinary join's hazard.

The classifier is deliberately oracle-free: it decides each form structurally so any consumer
can call it without threading the nullability substrate. The one edge that needs nullability,
a ``LEFT JOIN ... IS NULL`` on a column that is not a join key, is left unrecognised here (see
:func:`_left_is_null_anti_join`); a consumer that holds the substrate decides it itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import _sqlglot as sg


class AntiJoinForm(StrEnum):
    """The surface form an anti-join was written in. All four denote the same operator."""

    NATIVE = "native"  # L ANTI JOIN R ON P
    NOT_EXISTS = "not_exists"  # ... FROM L WHERE NOT EXISTS (SELECT ... FROM R WHERE P)
    NOT_IN = "not_in"  # ... FROM L WHERE l.k NOT IN (SELECT r FROM R)
    LEFT_IS_NULL = "left_is_null"  # L LEFT JOIN R ON P WHERE R.<join key> IS NULL


@dataclass(frozen=True)
class AntiJoin:
    """One anti-join: the rows of ``probe_alias`` (``L``) with no matching row in
    ``matched_name`` (``R``) on the predicate that equates ``probe_cols`` to ``matched_cols``.

    ``probe_cols`` / ``matched_cols`` are the bare (lower-cased) columns of a clean equality
    predicate, empty when the predicate does not decode to column equalities (a native anti-join
    still filters, so its form is reported even when its columns are not readable). ``matched_name``
    is ``R``'s bare table name, or ``None`` when ``R`` is a subquery or derived table. ``join`` is
    the join-arm node for the forms that have one (``NATIVE`` / ``LEFT_IS_NULL``), so a per-join
    reducer can match it by identity; it is ``None`` for the predicate forms. ``node`` is the anchor
    a finding attaches to (the join arm, or the ``NOT EXISTS`` / ``NOT IN`` predicate), always set.
    """

    form: AntiJoinForm
    probe_alias: str
    probe_cols: frozenset[str]
    matched_name: str | None
    matched_cols: frozenset[str]
    node: Expr
    join: exp.Join | None = None


def anti_joins_of(sel: exp.Select) -> tuple[AntiJoin, ...]:
    """Every anti-join whose probe side is ``sel``'s own FROM/JOIN scope.

    Reads only this scope's join arms and top-level WHERE conjuncts, never descending into a
    subquery (each nested SELECT is its own scope for the caller to visit). Order is join arms
    first, then WHERE predicates, so a scope with several anti-joins reports them deterministically.
    """
    from_ = sg.from_of(sel)
    if from_ is None or from_.this is None:
        return ()
    from_alias = from_.this.alias_or_name

    out: list[AntiJoin] = []
    where = sg.where_of(sel)
    leaves = sg.conjunctive_leaves(where.this) if where is not None and where.this else []

    for j in sg.joins_of(sel):
        side = sg.join_side_of(j)
        if side is sg.JoinSide.ANTI:
            out.append(_join_anti(j, AntiJoinForm.NATIVE, from_alias=from_alias))
        elif side is sg.JoinSide.LEFT:
            recognised = _left_is_null_anti_join(j, leaves=leaves, from_alias=from_alias)
            if recognised is not None:
                out.append(recognised)

    for leaf in leaves:
        if not isinstance(leaf, exp.Not) or not isinstance(leaf.this, Expr):
            continue
        inner = leaf.this
        if isinstance(inner, exp.Exists):
            not_exists = _not_exists_anti_join(inner, from_alias=from_alias, node=leaf)
            if not_exists is not None:
                out.append(not_exists)
        elif isinstance(inner, exp.In):
            not_in = _not_in_anti_join(inner, from_alias=from_alias, node=leaf)
            if not_in is not None:
                out.append(not_in)

    return tuple(out)


def _sides_of(on: Expr | None) -> dict[str, frozenset[str]]:
    """Per-alias equality columns of a join/correlation predicate, lower-cased, empty when the
    predicate is not a clean conjunction of column equalities."""
    by_alias = sg.equality_cols_by_alias(on) if on is not None else None
    if by_alias is None:
        return {}
    return {alias: frozenset(c.lower() for c in cols) for alias, cols in by_alias.items()}


def _probe_split(
    sides: dict[str, frozenset[str]], *, matched_alias: str, from_alias: str
) -> tuple[str, frozenset[str], frozenset[str]]:
    """Split decoded equality sides into ``(probe_alias, probe_cols, matched_cols)``. The probe is
    the single alias other than ``matched_alias``; with none or several other aliases the probe
    columns do not decode cleanly, so they fall back to ``from_alias`` with no columns."""
    matched_cols = sides.get(matched_alias, frozenset())
    others = {a: c for a, c in sides.items() if a != matched_alias}
    if len(others) == 1:
        ((probe_alias, probe_cols),) = others.items()
        return probe_alias, probe_cols, matched_cols
    return from_alias, frozenset(), matched_cols


def _join_anti(j: exp.Join, form: AntiJoinForm, *, from_alias: str) -> AntiJoin:
    target = j.this
    matched_alias = target.alias_or_name
    matched_name = target.name if isinstance(target, exp.Table) else None
    sides = _sides_of(sg.on_of(j))
    probe_alias, probe_cols, matched_cols = _probe_split(
        sides, matched_alias=matched_alias, from_alias=from_alias
    )
    return AntiJoin(form, probe_alias, probe_cols, matched_name, matched_cols, node=j, join=j)


def _left_is_null_anti_join(j: exp.Join, *, leaves: list[Expr], from_alias: str) -> AntiJoin | None:
    """A ``LEFT JOIN R ON P WHERE R.<col> IS NULL`` is an anti-join only when the ``IS NULL``
    column is one of ``R``'s join-key columns in ``P``. A matched row then has ``l.x = R.col`` so
    ``R.col`` is non-NULL there and the filter drops it, keeping exactly the unmatched rows. The
    equality match proves the column non-NULL, so the recognition needs no nullability oracle. An
    ``IS NULL`` on a non-key column can leak matched rows and is left for a substrate-backed
    consumer to decide."""
    target = j.this
    matched_alias = target.alias_or_name
    sides = _sides_of(sg.on_of(j))
    matched_cols = sides.get(matched_alias, frozenset())
    if not matched_cols:
        return None
    for leaf in leaves:
        if not isinstance(leaf, exp.Is) or not isinstance(leaf.expression, exp.Null):
            continue
        col = leaf.this
        if not isinstance(col, exp.Column):
            continue
        if sg.column_table(col) == matched_alias and sg.column_name(col).lower() in matched_cols:
            probe_alias, probe_cols, _ = _probe_split(
                sides, matched_alias=matched_alias, from_alias=from_alias
            )
            matched_name = target.name if isinstance(target, exp.Table) else None
            return AntiJoin(
                AntiJoinForm.LEFT_IS_NULL,
                probe_alias,
                probe_cols,
                matched_name,
                matched_cols,
                node=j,
                join=j,
            )
    return None


def _not_exists_anti_join(exists: exp.Exists, *, from_alias: str, node: Expr) -> AntiJoin | None:
    """``NOT EXISTS (SELECT ... FROM R WHERE P)`` correlated to the outer probe. ``R`` is the
    subquery's FROM relation; ``P``'s equalities split into the inner ``R`` columns and the outer
    probe columns."""
    inner = exists.this
    if not isinstance(inner, exp.Select):
        return None
    inner_from = sg.from_of(inner)
    if inner_from is None or inner_from.this is None:
        return None
    matched_alias = inner_from.this.alias_or_name
    matched_name = inner_from.this.name if isinstance(inner_from.this, exp.Table) else None
    inner_where = sg.where_of(inner)
    sides = _sides_of(inner_where.this if inner_where is not None else None)
    probe_alias, probe_cols, matched_cols = _probe_split(
        sides, matched_alias=matched_alias, from_alias=from_alias
    )
    return AntiJoin(
        AntiJoinForm.NOT_EXISTS, probe_alias, probe_cols, matched_name, matched_cols, node=node
    )


def _not_in_anti_join(in_node: exp.In, *, from_alias: str, node: Expr) -> AntiJoin | None:
    """``l.k NOT IN (SELECT r FROM R)`` over a single bare projected column of a single bare
    table. The probe column is the ``NOT IN`` left side; the matched column is the subquery's one
    projection."""
    query = in_node.args.get("query")
    if query is None:
        return None
    probe_col = in_node.this
    if not isinstance(probe_col, exp.Column):
        return None
    resolved = single_projected_column(query)
    if resolved is None:
        return None
    matched_name, matched_col = resolved
    probe_alias = sg.column_table(probe_col) or from_alias
    return AntiJoin(
        AntiJoinForm.NOT_IN,
        probe_alias,
        frozenset({sg.column_name(probe_col).lower()}),
        matched_name,
        frozenset({matched_col}),
        node=node,
    )


def single_projected_column(query: Expr) -> tuple[str, str] | None:
    """The ``(relation, column)`` a subquery projects, when it is a single bare column over a
    single bare-table FROM with no joins; else ``None``. The lower-cased column matches the
    nullability substrate's own column keys."""
    select = query.this if isinstance(query, exp.Subquery) else query
    if not isinstance(select, exp.Select) or sg.joins_of(select):
        return None
    projections = select.selects
    if len(projections) != 1:
        return None
    proj = projections[0]
    column = proj.this if isinstance(proj, exp.Alias) else proj
    if not isinstance(column, exp.Column):
        return None
    from_ = sg.from_of(select)
    if from_ is None or not isinstance(from_.this, exp.Table):
        return None
    return (from_.this.name, sg.column_name(column).lower())
