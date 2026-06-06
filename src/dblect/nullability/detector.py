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

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.facts.model import Annotation
from dblect.lineage.graph import ColumnRef, SourceKind
from dblect.lineage.properties import Nullability
from dblect.lineage.properties.nullability import activated_nullability
from dblect.manifest import Manifest
from dblect.sql import Finding, FindingKind
from dblect.sql import _sqlglot as sg

Detector = Callable[[Expr], tuple[Finding, ...]]

# Per relation name (as it appears in compiled SQL), the columns proven NULLABLE.
NullableByName = Mapping[str, frozenset[str]]


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


def _finding(grp_expr: exp.Column, *, source: str, column: str) -> Finding:
    rendered = sg.render_sql(grp_expr)
    span = sg.line_range(grp_expr)
    line_start, line_end = span if span is not None else (0, 0)
    return Finding(
        kind=FindingKind.NULL_GROUP_ON_NULLABLE_KEY,
        message=(
            f"GROUP BY {rendered} groups on a column that is nullable upstream in "
            f"{source!r}; rows with a NULL {column} collapse into a single phantom group "
            f"that downstream code rarely accounts for. Filter the nulls before grouping, "
            f"or make the orphan-handling intent explicit."
        ),
        sql_snippet=rendered,
        line_start=line_start,
        line_end=line_end,
    )


def _nullable_by_name(
    manifest: Manifest, anns: Mapping[ColumnRef, Annotation[Nullability]]
) -> dict[str, frozenset[str]]:
    """Index the proven-NULLABLE columns by the relation name as it appears in compiled
    SQL, mirroring the uniqueness detector's name resolution: a source resolves under
    ``identifier or name``, a model under ``name``, and a model wins on a name collision
    (as a ``ref`` would)."""
    sources: dict[str, set[str]] = {}
    models: dict[str, set[str]] = {}
    for col_ref, ann in anns.items():
        if ann.value is not Nullability.NULLABLE:
            continue
        node = manifest.nodes.get(col_ref.source.unique_id)
        if node is None:
            continue
        bucket = models if col_ref.source.kind is SourceKind.MODEL else sources
        bucket.setdefault(node.identifier or node.name, set()).add(col_ref.column)
    merged: dict[str, set[str]] = {name: set(cols) for name, cols in sources.items()}
    merged.update(models)  # a model wins on a name collision, as a ref would
    return {name: frozenset(cols) for name, cols in merged.items()}


def make_nullability_detectors(
    manifest: Manifest, *, dialect: str | None = "duckdb", parsed: Mapping[str, Expr] | None = None
) -> tuple[Detector, ...]:
    """Curry the nullability-consuming detectors against the propagated annotations.

    Runs one cross-model nullability propagation (outer-join taint plus conditional
    activation, via ``activated_nullability``), indexes the proven-NULLABLE columns by
    relation name, and currys the GROUP BY detector. ``dialect`` and ``parsed`` are
    accepted for symmetry with ``make_fact_grounded_detectors`` (the walker passes the
    pre-parsed trees); this detector reads only the per-relation index.
    """
    nullable_by_name = _nullable_by_name(manifest, activated_nullability(manifest, parsed=parsed))

    def group_by_nullable(tree: Expr) -> tuple[Finding, ...]:
        return detect_null_group_on_nullable_key(tree, nullable_by_name=nullable_by_name)

    return (group_by_nullable,)
