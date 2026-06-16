"""Manifest-grounded audit detector for snapshot reads missing a temporal filter.

A dbt snapshot keeps every historical version of each key (SCD-2), so a query that
reads it without restricting to the current row (``dbt_valid_to IS NULL``) or a
point-in-time slice (``BETWEEN dbt_valid_from AND dbt_valid_to``) fans out one row
per version, most visibly under a JOIN.

``detect_snapshot_temporal_filter`` judges a reference against a map of snapshot
relation name to its SCD-2 validity columns, so it never fires without manifest
knowledge that the relation is a snapshot and it honors a snapshot that renamed its
validity columns via ``snapshot_meta_column_names``. ``make_snapshot_detectors``
curries that detector against the manifest's snapshots, the same shape as the other
manifest-grounded detector families (``make_fact_grounded_detectors``,
``make_nullability_detectors``), so the audit walker composes it without owning any
snapshot-specific wiring.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.manifest import DEFAULT_SNAPSHOT_VALIDITY_COLUMNS, Manifest
from dblect.sql import Finding, FindingKind, finding_at

Detector = Callable[[Expr], tuple[Finding, ...]]


def make_snapshot_detectors(manifest: Manifest) -> tuple[Detector, ...]:
    """The snapshot temporal-filter detector, curried against the manifest's snapshots.

    Each snapshot is keyed by ``name`` (matching the relation-graph builder) and mapped
    to its SCD-2 validity columns, so a snapshot that renamed them via
    ``snapshot_meta_column_names`` is checked against its real column names. A snapshot
    whose config carries no validity columns (an older manifest without the
    ``snapshot_meta_column_names`` block) falls back to dbt's defaults. Returns nothing
    when the project has no snapshots, so the detector never fires without manifest
    knowledge that a referenced relation is a snapshot.
    """
    snapshots: dict[str, tuple[str, ...]] = {}
    for node in manifest.snapshots.values():
        validity = node.config.snapshot_validity_columns if node.config else ()
        snapshots[node.name.lower()] = validity or DEFAULT_SNAPSHOT_VALIDITY_COLUMNS
    if not snapshots:
        return ()

    def snapshot_temporal(tree: Expr) -> tuple[Finding, ...]:
        return detect_snapshot_temporal_filter(tree, snapshots=snapshots)

    return (snapshot_temporal,)


def detect_snapshot_temporal_filter(
    tree: Expr, *, snapshots: Mapping[str, tuple[str, ...]]
) -> tuple[Finding, ...]:
    """Flag a reference to a snapshot whose enclosing query omits a temporal filter.

    A snapshot keeps every historical version of each key, so a query that reads it
    without restricting to the current row or a point-in-time slice fans out one row
    per version, most visibly under a JOIN. ``snapshots`` maps each snapshot relation
    name (case-folded) to its SCD-2 validity columns as ``(valid_from, valid_to)``,
    honoring a snapshot's ``snapshot_meta_column_names`` rename. A reference to
    anything not in the map is ignored, so the detector never fires without manifest
    knowledge that the relation is a snapshot.

    Conservative toward silence on the validity columns: if any enclosing scope (the
    immediate SELECT, or an outer query reading it through a CTE or subquery) filters
    on one of that snapshot's validity columns, the developer is handling temporality
    and nothing fires. One finding per snapshot reference per scope.
    """
    if not snapshots:
        return ()
    out: list[Finding] = []
    seen: set[tuple[int, str]] = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        validity = snapshots.get(name)
        if validity is None:
            continue
        selects = list(_enclosing_selects(table))
        if not selects:
            continue
        key = (id(selects[0]), name)
        if key in seen:
            continue
        seen.add(key)
        validity_lc = frozenset(c.lower() for c in validity)
        if any(_predicates_mention(select, validity_lc) for select in selects):
            continue
        out.append(
            finding_at(
                FindingKind.SNAPSHOT_TEMPORAL_FILTER_MISSING,
                message=(
                    f"`{table.name}` is a dbt snapshot, read here without a filter on its "
                    "SCD-2 validity columns. It keeps every historical version per key, so "
                    "this query fans out one row per version (most visibly under a JOIN). "
                    f"{_validity_remedy(validity)} If reading full history is intended, "
                    "suppress with `-- noqa-fixture:`."
                ),
                node=table,
            )
        )
    return tuple(out)


def _select_predicates(select: exp.Select) -> tuple[Expr, ...]:
    """The predicate-bearing clauses of one SELECT: WHERE, every JOIN ON, QUALIFY,
    and HAVING. These are where a temporal filter on a snapshot would live; a
    validity column in a projection or GROUP BY does not restrict the row set."""
    out: list[Expr] = []
    for key in ("where", "qualify", "having"):
        clause = select.args.get(key)
        if isinstance(clause, Expr):
            out.append(clause)
    for join in select.args.get("joins") or ():
        if isinstance(join, exp.Join):
            on = join.args.get("on")
            if isinstance(on, Expr):
                out.append(on)
    return tuple(out)


def _enclosing_selects(node: Expr) -> Iterable[exp.Select]:
    """Each SELECT enclosing ``node``, innermost first. A snapshot read in a CTE or
    subquery whose rows are restricted by the outer query is safe, so the temporal
    filter may live in any of these scopes, not only the immediately enclosing one."""
    current = node.parent
    while current is not None:
        if isinstance(current, exp.Select):
            yield current
        current = current.parent


def _predicates_mention(select: exp.Select, columns: frozenset[str]) -> bool:
    """Whether the SELECT's predicates reference any of ``columns`` (case-folded)."""
    return any(
        col.name.lower() in columns
        for clause in _select_predicates(select)
        for col in clause.find_all(exp.Column)
    )


def _validity_remedy(validity: tuple[str, ...]) -> str:
    """The remediation sentence naming a snapshot's own validity columns, so the
    advice matches a renamed snapshot rather than always citing the dbt defaults."""
    if len(validity) == 2:
        valid_from, valid_to = validity
        return (
            f"Restrict to the current row with `{valid_to} IS NULL`, or to a point in "
            f"time with `BETWEEN {valid_from} AND {valid_to}`."
        )
    named = ", ".join(f"`{c}`" for c in validity)
    return f"Restrict on its SCD-2 validity columns ({named})."
