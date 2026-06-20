"""Fact-grounded audit detector that consumes the nullability property.

``detect_null_group_on_nullable_key`` flags a GROUP BY on a column the nullability
property proved NULLABLE upstream. Such a column collapses every NULL row into one
phantom group the consumer rarely models (the jaffle ``customers.sql`` shape, lifted
across model boundaries). It is the cross-model complement to the structural
``null_group_after_outer_join``: that detector needs the outer join in the same
SELECT, this one fires when the nullability was inherited from an upstream model,
which the local AST cannot see.

The two never double-flag, because this detector reasons only about single-source,
join-free scopes. With no join in the scope, any nullability in the group key must
have come from upstream, so the local-outer-join case stays entirely with the
structural detector. It is opportunistic like the uniqueness detectors: it fires only
on a column proven NULLABLE and stays silent everywhere else (the firewall, so an
undeclared project sees no noise).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.lineage.facts.model import Annotation
from dblect.lineage.graph import ColumnRef, SourceKind
from dblect.lineage.properties import Nullability
from dblect.lineage.properties.nullability import (
    activated_nullability,
    outer_join_nullable_columns,
)
from dblect.manifest import Manifest
from dblect.sql import Finding, FindingKind, finding_at
from dblect.sql import _sqlglot as sg
from dblect.sql._sqlglot import JoinSide

Detector = Callable[[Expr], tuple[Finding, ...]]

# Per relation name (as it appears in compiled SQL), the columns proven NULLABLE.
NullableByName = Mapping[str, frozenset[str]]


class NullableCause(StrEnum):
    """Why a column is nullable upstream, when the substrate can attribute it.

    The ``*_JOIN`` causes are positive structural claims the nullability substrate already
    derives (the column is drawn from an outer join's optional side), named by the join kind
    that padded it. ``UNKNOWN`` is the honest fallback: the column is proven NULLABLE, but
    the cause is not derivable here, so the finding names no cause rather than fabricating
    one."""

    LEFT_JOIN = "left_join"
    RIGHT_JOIN = "right_join"
    FULL_JOIN = "full_join"
    UNKNOWN = "unknown"


# Per relation name, the column-to-cause map for proven-NULLABLE columns whose cause the
# substrate attributes. A column absent from the inner map reads as ``UNKNOWN``.
CauseByName = Mapping[str, Mapping[str, NullableCause]]


def detect_null_group_on_nullable_key(
    tree: Expr, *, nullable_by_name: NullableByName
) -> tuple[Finding, ...]:
    """Flag GROUP BY targets that group on a column nullable in the upstream relation.

    A scope is checkable when its FROM is a single relation with no JOINs, so any
    nullability in the group key was inherited rather than introduced locally (the
    local case belongs to ``null_group_after_outer_join``). Only bare-column group keys
    are reasoned about; a computed key like ``date_trunc(col)`` needs an equivalence we
    do not model and is skipped.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        from_ = sg.from_of(sel)
        if from_ is None or sg.joins_of(sel):
            continue
        target = from_.this
        if not isinstance(target, exp.Table):
            continue
        nullable = nullable_by_name.get(target.name)
        if not nullable:
            continue
        group = sg.group_of(sel)
        if group is None:
            continue
        for grp_expr in group.expressions:
            if not isinstance(grp_expr, exp.Column):
                continue
            qualifier = sg.column_table(grp_expr)
            if qualifier is not None and qualifier.lower() != target.alias_or_name.lower():
                continue
            column = sg.column_name(grp_expr).lower()
            if column in nullable:
                out.append(_finding(grp_expr, source=target.name, column=column))
    return tuple(out)


def detect_join_on_nullable_key(
    tree: Expr, *, nullable_by_name: NullableByName, cause_by_name: CauseByName | None = None
) -> tuple[Finding, ...]:
    """Flag a JOIN whose equality key is a column nullable in its upstream relation.

    NULL never equals NULL, so rows with a NULL join key never match: an inner join
    silently drops them and an outer join leaves them unmatched. As with the group-by
    detector, the nullability is read from the upstream relation, so this fires on an
    inherited-nullable key the local SQL gives no hint about, complementing the
    structural ``coalesce_on_join_key`` and ``where_on_outer_joined_nullable``. Only
    bare-column equality keys are reasoned about.
    """
    causes_by_relation = cause_by_name or {}
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        alias_to_rel = _alias_to_relation(sel)
        for join in sg.joins_of(sel):
            on = sg.on_of(join)
            if on is None:
                continue
            for alias, relation in alias_to_rel.items():
                nullable = nullable_by_name.get(relation)
                if not nullable:
                    continue
                cols = sg.equality_cols_on_alias(on, alias)
                if not cols:
                    continue
                causes = causes_by_relation.get(relation, {})
                out.extend(
                    _join_finding(
                        join,
                        source=relation,
                        column=column,
                        cause=causes.get(column, NullableCause.UNKNOWN),
                    )
                    for column in sorted(cols & nullable)
                )
    return tuple(out)


def detect_not_in_nullable_subquery(
    tree: Expr, *, nullable_by_name: NullableByName
) -> tuple[Finding, ...]:
    """Flag ``x NOT IN (SELECT col FROM rel)`` where ``col`` is nullable in ``rel``.

    A single NULL among the subquery's values makes ``x NOT IN (...)`` evaluate to NULL
    for every ``x``, so the predicate is never true and the result is silently empty:
    the canonical three-valued-logic footgun. Only the single-bare-column, single-source
    subquery shape is reasoned about; anything else is left alone.
    """
    out: list[Finding] = []
    for in_node in tree.find_all(exp.In):
        query = in_node.args.get("query")
        if query is None or not isinstance(in_node.parent, exp.Not):
            continue
        resolved = _single_projected_column(query)
        if resolved is None:
            continue
        relation, column = resolved
        nullable = nullable_by_name.get(relation)
        if nullable and column in nullable:
            out.append(_not_in_finding(in_node, source=relation, column=column))
    return tuple(out)


def _alias_to_relation(sel: exp.Select) -> dict[str, str]:
    """Map each FROM/JOIN alias to its bare table name. Subquery and CTE sources are
    skipped (their per-scope nullability is a later increment)."""
    out: dict[str, str] = {}
    from_ = sg.from_of(sel)
    if from_ is not None and isinstance(from_.this, exp.Table):
        out[from_.this.alias_or_name] = from_.this.name
    for join in sg.joins_of(sel):
        target = join.this
        if isinstance(target, exp.Table):
            out[target.alias_or_name] = target.name
    return out


def _single_projected_column(query: Expr) -> tuple[str, str] | None:
    """The ``(relation, column)`` a subquery projects, when it is a single bare column
    over a single bare-table FROM with no joins; else ``None``."""
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


def _finding(grp_expr: exp.Column, *, source: str, column: str) -> Finding:
    return finding_at(
        FindingKind.NULL_GROUP_ON_NULLABLE_KEY,
        message=(
            f"GROUP BY {sg.render_sql(grp_expr)} groups on a column that is nullable upstream in "
            f"{source!r}; rows with a NULL {column} collapse into a single phantom group "
            f"that downstream code rarely accounts for. Filter the nulls before grouping, "
            f"or make the orphan-handling intent explicit."
        ),
        node=grp_expr,
    )


def _cause_clause(cause: NullableCause) -> str:
    """The ``why nullable`` clause, when the substrate attributes a cause. Returns an empty
    string for ``UNKNOWN`` so the message degrades honestly rather than fabricating one."""
    match cause:
        case NullableCause.LEFT_JOIN:
            return " (produced via a left join, so unmatched rows leave it NULL)"
        case NullableCause.RIGHT_JOIN:
            return " (produced via a right join, so unmatched rows leave it NULL)"
        case NullableCause.FULL_JOIN:
            return " (produced via a full join, so unmatched rows leave it NULL)"
        case NullableCause.UNKNOWN:
            return ""


def _join_finding(
    join: exp.Join, *, source: str, column: str, cause: NullableCause = NullableCause.UNKNOWN
) -> Finding:
    return finding_at(
        FindingKind.JOIN_ON_NULLABLE_KEY,
        message=(
            f"JOIN keys on {column}, which is nullable upstream in {source!r}"
            f"{_cause_clause(cause)}; NULL never equals NULL, so rows with a NULL {column} "
            f"never match and are silently dropped (inner join) or left unmatched (outer "
            f"join). The durable guard is a not_null test on {column} in {source!r}, which "
            f"turns this silent row loss into a loud test failure on the producing model; "
            f"or, locally, filter the nulls or COALESCE to a sentinel if the match was "
            f"intended."
        ),
        node=join,
    )


def _not_in_finding(in_node: exp.In, *, source: str, column: str) -> Finding:
    return finding_at(
        FindingKind.NOT_IN_NULLABLE_SUBQUERY,
        message=(
            f"NOT IN over a subquery projecting {column}, which is nullable upstream in "
            f"{source!r}; one NULL makes the whole predicate never true, so the result is "
            f"silently empty. Use NOT EXISTS, or filter the NULLs from the subquery."
        ),
        node=in_node,
    )


def _nullable_by_name(
    manifest: Manifest, anns: Mapping[ColumnRef, Annotation[Nullability]]
) -> dict[str, frozenset[str]]:
    """Index the proven-NULLABLE columns by the relation name as it appears in compiled
    SQL, mirroring the uniqueness detector's name resolution: a source resolves under
    ``identifier or name``, a model under ``name``, and a model wins on a name collision
    (as a ``ref`` would). Column names are lowercased so the index matches the detectors'
    lowercased AST keys on a dialect that case-folds bare identifiers."""
    sources: dict[str, set[str]] = {}
    models: dict[str, set[str]] = {}
    for col_ref, ann in anns.items():
        if ann.value is not Nullability.NULLABLE:
            continue
        node = manifest.nodes.get(col_ref.source.unique_id)
        if node is None:
            continue
        bucket = models if col_ref.source.kind is SourceKind.MODEL else sources
        bucket.setdefault(node.identifier or node.name, set()).add(col_ref.column.lower())
    merged: dict[str, set[str]] = {name: set(cols) for name, cols in sources.items()}
    merged.update(models)  # a model wins on a name collision, as a ref would
    return {name: frozenset(cols) for name, cols in merged.items()}


_CAUSE_OF_SIDE: Mapping[JoinSide, NullableCause] = {
    JoinSide.LEFT: NullableCause.LEFT_JOIN,
    JoinSide.RIGHT: NullableCause.RIGHT_JOIN,
    JoinSide.FULL: NullableCause.FULL_JOIN,
}


def _cause_by_name(
    manifest: Manifest, *, parsed: Mapping[str, Expr] | None = None
) -> dict[str, dict[str, NullableCause]]:
    """Index, by relation name, the attributable ``why nullable`` cause per column.

    Only the outer-join cause is attributable today, read from the same structural
    analysis the nullability taint uses (:func:`outer_join_nullable_columns`). A column
    the substrate cannot attribute is simply absent, so the finding reads it as
    ``UNKNOWN`` and names no cause."""
    out: dict[str, dict[str, NullableCause]] = {}
    for name, columns in outer_join_nullable_columns(manifest, parsed=parsed).items():
        out[name] = {col: _CAUSE_OF_SIDE[side] for col, side in columns.items()}
    return out


def make_nullability_detectors(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
) -> tuple[Detector, ...]:
    """Curry the nullability-consuming detectors against the propagated annotations.

    Runs one cross-model nullability propagation (outer-join taint plus conditional
    activation, via ``activated_nullability``), indexes the proven-NULLABLE columns by
    relation name, and curries the GROUP BY, join-key, and NOT-IN detectors against that
    index. ``profile`` is the run's resolved target (its dialect parses, its semantics
    ground); ``parsed`` lets the walker share its pre-parsed trees. The detectors read
    only the per-relation index.
    """
    nullable_by_name = _nullable_by_name(
        manifest, activated_nullability(manifest, profile, parsed=parsed)
    )
    cause_by_name = _cause_by_name(manifest, parsed=parsed)

    def group_by_nullable(tree: Expr) -> tuple[Finding, ...]:
        return detect_null_group_on_nullable_key(tree, nullable_by_name=nullable_by_name)

    def join_on_nullable(tree: Expr) -> tuple[Finding, ...]:
        return detect_join_on_nullable_key(
            tree, nullable_by_name=nullable_by_name, cause_by_name=cause_by_name
        )

    def not_in_nullable(tree: Expr) -> tuple[Finding, ...]:
        return detect_not_in_nullable_subquery(tree, nullable_by_name=nullable_by_name)

    return (group_by_nullable, join_on_nullable, not_in_nullable)
