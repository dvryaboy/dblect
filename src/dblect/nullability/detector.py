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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.lineage.facts.model import Annotation
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceKind
from dblect.lineage.properties import Nullability
from dblect.lineage.properties.functional_dependency import NO_FDS, FDSet, minimal_cover
from dblect.lineage.properties.nullability import (
    activated_nullability,
    outer_join_nullable_columns,
)
from dblect.manifest import Manifest
from dblect.sql import Finding, FindingKind, anti_join, finding_at
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


@dataclass(frozen=True)
class _NullableKey:
    """One join-key column proven nullable upstream, with its source relation and cause.

    A composite-key join collects several of these for one finding, so the message can list
    every spanned column instead of fanning out one finding per column."""

    relation: str
    column: str
    cause: NullableCause


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
    tree: Expr,
    *,
    nullable_by_name: NullableByName,
    cause_by_name: CauseByName | None = None,
    fd_by_name: Mapping[str, FDSet] = {},
) -> tuple[Finding, ...]:
    """Flag a JOIN whose equality key is a column nullable in its upstream relation.

    NULL never equals NULL, so a NULL join key never matches, but the consequence depends
    on the join. An inner join drops the row from either side, silent row loss. An outer
    join keeps its preserved rows and pads the target with NULLs, a silent non-match; its
    dropped side simply not joining is the join's defining semantics rather than a hazard,
    and ``where_on_outer_joined_nullable`` and ``null_group_on_nullable_key`` already cover
    that side's downstream effects with more precision. So this gates per join on
    ``joins_with_outer_dropped_aliases``: it flags every nullable key except the one on the
    join's own dropped side. A FULL join drops nothing (both sides survive NULL-padded), so
    it flags both. A semi join filters its left rows, so a NULL key on either side silently
    drops the row exactly as an inner join does, and it is flagged with the same row-loss
    framing. An anti join inverts the hazard: a NULL probe key matches nothing, so the row is
    kept as a spurious non-match rather than dropped, and only the probe side is flagged (a
    NULL on the matched side is null-safe, simply failing to match, unlike NOT IN). Gating per
    join keeps an outer join elsewhere in the same SELECT from silencing an inner join's
    genuine row loss.

    Nullability is read from the upstream relation, so this fires on an inherited-nullable
    key the local SQL gives no hint about. One join is one decision to look at, so a
    composite-key join yields one finding listing the spanned key columns rather than one
    per column. Only bare-column equality keys are reasoned about.

    When a declared ``determines`` chain relates the spanned columns (``store_id ->
    region_id -> country_id``), the join is really keyed on the chain's root: the additional
    equalities are functionally redundant. The nullable columns are reduced to their
    :func:`minimal_cover` under the relation's ``fd_by_name``, so the finding reports the
    declared key as one unit and points at dropping the redundant conditions rather than
    treating co-determined columns as independent hazards. The reduction never silences the
    finding (every folded column stays covered by a still-nullable root, so a genuine
    null-non-match risk remains); only a non-null key, absent from ``nullable_by_name``,
    yields silence. With no dependency known the cover is the columns unchanged, so an
    undeclared project reads exactly as before.
    """
    causes_by_relation = cause_by_name or {}
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        alias_to_rel = _alias_to_relation(sel)
        # An anti-join reverses the hazard, so it is decided by the shared classifier and the
        # probe side alone; the classifier keys each anti-join arm by its Join node.
        anti_by_join = {id(a.join): a for a in anti_join.anti_joins_of(sel) if a.join is not None}
        for join, side, dropped in sg.joins_with_outer_dropped_aliases(sel):
            on = sg.on_of(join)
            if on is None:
                continue
            if side is JoinSide.ANTI:
                # A native anti join keeps a NULL-PROBE-key row as a spurious non-match (it
                # matches nothing), the inverse of a dropped row. Only the probe side is a
                # hazard; a NULL on the matched side simply fails to match, which is the
                # anti-join's null-safe semantics (unlike NOT IN, whose empty-result footgun
                # ``detect_not_in_nullable_subquery`` covers). The matched side of ``LEFT JOIN
                # ... IS NULL`` reaches this detector as an ordinary LEFT join instead.
                anti = anti_by_join.get(id(join))
                if anti is None:  # ON is not a clean conjunction of column equalities
                    continue
                keys, determined = _nullable_keys_for_alias(
                    anti.probe_alias,
                    anti.probe_cols,
                    alias_to_rel=alias_to_rel,
                    nullable_by_name=nullable_by_name,
                    causes_by_relation=causes_by_relation,
                    fd_by_name=fd_by_name,
                )
                if keys:
                    out.append(_join_finding(join, keys, determined=determined, side=side))
                continue
            cols_by_alias = sg.equality_cols_by_alias(on)
            if cols_by_alias is None:  # not a clean conjunction of column equalities
                continue
            keys: list[_NullableKey] = []
            determined: list[_NullableKey] = []
            for alias, cols in sorted(cols_by_alias.items()):
                if alias in dropped:  # this outer join drops the no-match here: its intent
                    continue
                k, d = _nullable_keys_for_alias(
                    alias,
                    cols,
                    alias_to_rel=alias_to_rel,
                    nullable_by_name=nullable_by_name,
                    causes_by_relation=causes_by_relation,
                    fd_by_name=fd_by_name,
                )
                keys.extend(k)
                determined.extend(d)
            if keys:
                out.append(_join_finding(join, keys, determined=determined, side=side))
    return tuple(out)


def _nullable_keys_for_alias(
    alias: str,
    cols: frozenset[str],
    *,
    alias_to_rel: Mapping[str, str],
    nullable_by_name: NullableByName,
    causes_by_relation: CauseByName,
    fd_by_name: Mapping[str, FDSet],
) -> tuple[list[_NullableKey], list[_NullableKey]]:
    """The nullable key columns an equality on ``alias`` contributes, split into the reported
    cover and the columns a declared ``determines`` folds into it.

    Empty lists when ``alias`` resolves to no bare relation (a subquery or CTE source) or the
    relation has no nullable column among ``cols``. The cover is the :func:`minimal_cover` of the
    nullable columns under the relation's FDs, falling back to the unreduced columns so a constant
    dependency (``{} -> c``) can never fold the last column away and silence the hazard; silence is
    only ever a non-null key's job."""
    relation = alias_to_rel.get(alias)
    if relation is None:
        return [], []
    nullable = nullable_by_name.get(relation)
    if not nullable:
        return [], []
    causes = causes_by_relation.get(relation, {})
    nullable_here = frozenset(cols & nullable)
    cover = minimal_cover(fd_by_name.get(relation, NO_FDS), nullable_here) or nullable_here
    keys = [
        _NullableKey(relation, column, causes.get(column, NullableCause.UNKNOWN))
        for column in sorted(cover)
    ]
    determined = [
        _NullableKey(relation, column, causes.get(column, NullableCause.UNKNOWN))
        for column in sorted(nullable_here - cover)
    ]
    return keys, determined


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
        resolved = anti_join.single_projected_column(query)
        if resolved is None:
            continue
        relation, column = resolved
        nullable = nullable_by_name.get(relation)
        if nullable and column in nullable:
            out.append(_not_in_finding(in_node, source=relation, column=column))
    return tuple(out)


def detect_not_exists_on_nullable_key(
    tree: Expr,
    *,
    nullable_by_name: NullableByName,
    cause_by_name: CauseByName | None = None,
    fd_by_name: Mapping[str, FDSet] = {},
) -> tuple[Finding, ...]:
    """Flag ``NOT EXISTS (SELECT ... FROM R WHERE P)`` whose probe correlation key is nullable.

    NOT EXISTS is the anti-join operator written as a correlated subquery, so it carries the same
    probe-side hazard as a native anti join: a NULL probe key makes the correlation predicate NULL
    for every inner row, so NOT EXISTS is true and the row is kept as a spurious non-match. The
    matched (inner) side is null-safe, which is exactly why NOT EXISTS is the recommended
    replacement for NOT IN, so only the probe side is flagged. The shared classifier decodes the
    correlation; the reduction and framing are the native anti join's."""
    causes_by_relation = cause_by_name or {}
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        alias_to_rel = _alias_to_relation(sel)
        for a in anti_join.anti_joins_of(sel):
            if a.form is not anti_join.AntiJoinForm.NOT_EXISTS:
                continue
            keys, determined = _nullable_keys_for_alias(
                a.probe_alias,
                a.probe_cols,
                alias_to_rel=alias_to_rel,
                nullable_by_name=nullable_by_name,
                causes_by_relation=causes_by_relation,
                fd_by_name=fd_by_name,
            )
            if keys:
                out.append(
                    _nullable_finding(
                        a.node, keys, determined=determined, prefix="NOT EXISTS", framing="anti"
                    )
                )
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


# The kind word prefixed onto "JOIN" in a finding. An inner join is absent and reads as the
# bare "JOIN". LEFT/RIGHT/FULL take the kept-row outer framing; SEMI takes the row-loss
# framing (it filters the left rows, so a NULL key drops the row exactly as an inner join);
# ANTI takes the inverted kept-as-spurious-non-match framing.
_JOIN_KIND_WORD: Mapping[JoinSide, str] = {
    JoinSide.LEFT: "LEFT",
    JoinSide.RIGHT: "RIGHT",
    JoinSide.FULL: "FULL OUTER",
    JoinSide.SEMI: "SEMI",
    JoinSide.ANTI: "ANTI",
}
_OUTER_SIDES = frozenset({JoinSide.LEFT, JoinSide.RIGHT, JoinSide.FULL})


def _columns_and_cause(keys: Sequence[_NullableKey]) -> tuple[str, str]:
    """The spanned key columns as a listing, and the trailing ``why nullable`` clause.

    Returns ``(listing, trailing_clause)``. When every column shares one cause the listing
    stays a clean ``k1, k2`` and the clause trails once (the common composite-key shape, all
    columns drawn from one upstream outer join). When the columns carry different causes the
    clause moves inline onto each column that has one, so no column borrows another's
    provenance, and the trailing clause is empty: ``k1 (produced via a left join, ...), k2``.

    A column drawn from more than one source with disagreeing causes resolves to no cause for
    that column, the same honest degradation applied per column."""
    seen: dict[str, set[NullableCause]] = {}
    for k in keys:
        seen.setdefault(k.column, set()).add(k.cause)
    resolved = {
        col: next(iter(causes)) if len(causes) == 1 else NullableCause.UNKNOWN
        for col, causes in seen.items()
    }
    columns = sorted(resolved)
    if len(set(resolved.values())) == 1:
        return ", ".join(columns), _cause_clause(next(iter(resolved.values())))
    return ", ".join(f"{col}{_cause_clause(resolved[col])}" for col in columns), ""


def _join_finding(
    join: exp.Join,
    keys: Sequence[_NullableKey],
    *,
    determined: Sequence[_NullableKey] = (),
    side: JoinSide,
) -> Finding:
    """One finding per join, reporting the nullable key columns it spans.

    The framing follows the join side. An inner or semi join drops the row on a NULL key, so
    the message keeps the row-loss framing (a semi join filters its left rows, so a NULL key
    on either side silently drops the left row just as an inner join would). An outer join's
    surviving rows reach here (the preserved side of a LEFT or RIGHT join, both sides of a
    FULL join), where the row is kept NULL-padded and the hazard is the silent non-match, so
    the message says the row is kept rather than implying any preserved-row loss.

    The ``why nullable`` cause is attributed per column: columns that agree share one trailing
    clause, columns that differ each carry their own inline, so a mixed-cause composite key
    stays one finding without one column's provenance bleeding onto another.

    ``keys`` are the reduced key (the :func:`minimal_cover` of the spanned nullable columns);
    ``determined`` are the nullable columns a declared ``determines`` folded into that key. When
    present, the message names them as functionally redundant and recommends dropping their
    equality conditions, which removes their null-non-match risk outright, ahead of a not_null
    test on the key that remains."""
    kind_word = _JOIN_KIND_WORD.get(side)
    prefix = f"{kind_word} JOIN" if kind_word is not None else "JOIN"
    framing = "anti" if side is JoinSide.ANTI else "outer" if side in _OUTER_SIDES else "row_loss"
    return _nullable_finding(join, keys, determined=determined, prefix=prefix, framing=framing)


def _nullable_finding(
    node: Expr,
    keys: Sequence[_NullableKey],
    *,
    determined: Sequence[_NullableKey],
    prefix: str,
    framing: str,
) -> Finding:
    """Assemble a nullable-key finding for a join or an anti-join predicate.

    ``framing`` selects the hazard wording: ``anti`` (a NULL key matches nothing so the row is
    kept as a spurious non-match), ``outer`` (a preserved row survives NULL-padded), or
    ``row_loss`` (the row is silently dropped). ``prefix`` names the construct (``LEFT JOIN``,
    ``ANTI JOIN``, ``NOT EXISTS``). The cause attribution, ``determines`` redundancy clause, and
    not_null guard are shared across every framing."""
    distinct_columns = sorted({k.column for k in keys})
    plain_columns = ", ".join(distinct_columns)
    listing, cause = _columns_and_cause(keys)
    sources = ", ".join(repr(r) for r in sorted({k.relation for k in keys}))
    is_are = "are" if len(distinct_columns) > 1 else "is"
    redundancy = _redundancy_clause(keys, determined)
    guard = (
        f"The durable guard is a not_null test on {plain_columns} in {sources}, which turns "
        f"this silent {{loss}} into a loud test failure on the producing model; or, locally, "
        f"filter the nulls or COALESCE to a sentinel if the match was intended."
    )
    head = f"{prefix} keys on {listing}, which {is_are} nullable upstream in {sources}{cause}; "
    if framing == "anti":
        body = (
            f"NULL never equals NULL, so a row with a NULL key matches nothing and is kept as a "
            f"spurious non-match, the anti-join including it rather than excluding "
            f"it.{redundancy} {guard.format(loss='spurious inclusion')}"
        )
    elif framing == "outer":
        body = (
            f"the outer join keeps these rows, but NULL never equals NULL, so a NULL key silently "
            f"never matches and the row survives with the join target "
            f"NULL-padded.{redundancy} {guard.format(loss='non-match')}"
        )
    else:
        body = (
            f"NULL never equals NULL, so rows with a NULL key never match and are silently "
            f"dropped.{redundancy} {guard.format(loss='row loss')}"
        )
    return finding_at(FindingKind.JOIN_ON_NULLABLE_KEY, message=head + body, node=node)


def _redundancy_clause(keys: Sequence[_NullableKey], determined: Sequence[_NullableKey]) -> str:
    """The clause naming the ``determines``-folded columns, when a declaration relates them.

    Empty when nothing was folded, so an undeclared join reads exactly as before. Otherwise it
    groups the folded columns by relation and names the declared key (the reported cover columns
    of that relation) that determines them, so the reader sees one declared key rather than
    several co-determined columns, and the recommended fix is to drop the redundant equalities."""
    if not determined:
        return ""
    cover_by_relation: dict[str, set[str]] = {}
    for k in keys:
        cover_by_relation.setdefault(k.relation, set()).add(k.column)
    determined_by_relation: dict[str, set[str]] = {}
    for k in determined:
        determined_by_relation.setdefault(k.relation, set()).add(k.column)
    parts = [
        f"{', '.join(sorted(cols))} in {relation!r} (functionally determined by the declared "
        f"key {', '.join(sorted(cover_by_relation.get(relation, set())))})"
        for relation, cols in sorted(determined_by_relation.items())
    ]
    return (
        f" The join also equates {'; '.join(parts)}, so those equality conditions are "
        f"redundant: joining on the declared key alone drops the null-non-match risk they add."
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
    column_graph: ColumnLineageGraph | None = None,
    fd_by_name: Mapping[str, FDSet] = {},
) -> tuple[Detector, ...]:
    """Curry the nullability-consuming detectors against the propagated annotations.

    Runs one cross-model nullability propagation (outer-join taint plus conditional
    activation, via ``activated_nullability``), indexes the proven-NULLABLE columns by
    relation name, and curries the GROUP BY, join-key, and NOT-IN detectors against that
    index. ``profile`` is the run's resolved target (its dialect parses, its semantics
    ground); ``parsed`` lets the walker share its pre-parsed trees. ``column_graph`` lets the
    audit pass the manifest column graph it already built, so the qualify-and-resolve walk is
    not repeated per fact family. ``fd_by_name`` is the propagated functional-dependency map
    (the same one the fanout detector reads), letting the join-key detector fold a co-determined
    key column into its declared key. The detectors read only the per-relation indexes.
    """
    nullable_by_name = _nullable_by_name(
        manifest, activated_nullability(manifest, profile, parsed=parsed, column_graph=column_graph)
    )
    cause_by_name = _cause_by_name(manifest, parsed=parsed)

    def group_by_nullable(tree: Expr) -> tuple[Finding, ...]:
        return detect_null_group_on_nullable_key(tree, nullable_by_name=nullable_by_name)

    def join_on_nullable(tree: Expr) -> tuple[Finding, ...]:
        return detect_join_on_nullable_key(
            tree,
            nullable_by_name=nullable_by_name,
            cause_by_name=cause_by_name,
            fd_by_name=fd_by_name,
        )

    def not_in_nullable(tree: Expr) -> tuple[Finding, ...]:
        return detect_not_in_nullable_subquery(tree, nullable_by_name=nullable_by_name)

    def not_exists_on_nullable(tree: Expr) -> tuple[Finding, ...]:
        return detect_not_exists_on_nullable_key(
            tree,
            nullable_by_name=nullable_by_name,
            cause_by_name=cause_by_name,
            fd_by_name=fd_by_name,
        )

    return (group_by_nullable, join_on_nullable, not_in_nullable, not_exists_on_nullable)
