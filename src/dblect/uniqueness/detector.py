"""Fact-grounded audit detectors that consume propagated lineage properties.

Opportunistic detectors (they fire only when the project gives enough information
to make a claim, and stay silent otherwise):

* ``detect_non_unique_window_order_keys``: window functions whose combined
  (partition, order) columns are not covered by any candidate key of the scope's
  single source. Ties in the ordering produce non-deterministic rankings.
* ``detect_non_unique_aggregate_order_keys``: the aggregate twin of the window check.
  A top-n ordered aggregate (``ARRAY_AGG(x ORDER BY k LIMIT n)``) whose (group, order)
  columns are not covered by a source key keeps an arbitrary winner among ties.
* ``detect_join_fanout``: JOINs whose joined-in side has known keys, none of
  which is covered by the join's equality predicate columns, so the join can
  multiply rows.
* ``detect_limit_without_deterministic_order``: a persisted model whose top-scope
  ``LIMIT`` has no ``ORDER BY``, or one whose order keys are not covered by a known
  uniqueness key, so a re-run materializes a different slice of rows.
* ``detect_cross_model_fanout``: a duplicate-sensitive aggregate that folds a
  magnitude an upstream fan-out replicated, over a relation no longer keyed at the
  magnitude's grain. This one also reads ``where_provenance`` to find the origin a
  magnitude traces to, the grain ``grain_preserved`` is asked about. A COUNT fold
  (``COUNT(*)``, ``COUNT(col)``) yields a cardinality, not a magnitude: it counts the
  relation's rows, whose grain the relation preserves, so it stays silent (the ``SUM(qty)``
  analog), unlike ``SUM(amount)``.

The first two read keys from the lineage.facts uniqueness substrate: per-model keys
come from cross-model propagation (``uniqueness_property`` over the relation graph),
and a per-tree scope index (``relation_scope_keys``) supplies the keys of CTE and
inline-subquery scopes, which are not relations the propagator annotates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import assert_never

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.lineage.builder import build_manifest_graph, build_relation_graph, index_by_name
from dblect.lineage.facts.model import Annotation, Fact, by_scope
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, RelationLineageGraph, SourceRef
from dblect.lineage.properties import where_provenance
from dblect.lineage.properties.functional_dependency import (
    NO_FDS,
    FDSet,
    determines,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.properties.predicate_flow import (
    predicate_flow_property,
    relation_scope_filters,
)
from dblect.lineage.properties.uniqueness import (
    NO_KEYS,
    CandidateKeySet,
    Key,
    activate_conditional,
    activated_scope_keys,
    grain_preserved,
    relation_scope_keys,
    uniqueness_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import Manifest, Materialization
from dblect.sql import (
    AggregateBehavior,
    Finding,
    FindingKind,
    aggregate_behavior,
    anti_join,
    duplicate_sensitive,
    suppression_hint,
)
from dblect.sql import _sqlglot as sg
from dblect.sql._sqlglot import JoinSide

Detector = Callable[[Expr], tuple[Finding, ...]]

# Per-model (and per-source) candidate keys, addressed by relation name as it
# appears in SQL. Per-scope keys are addressed by ``id(node)`` for the lifetime
# of one parsed tree.
ModelKeys = Mapping[str, frozenset[Key]]
ScopeIndex = Mapping[int, frozenset[Key]]


def detect_non_unique_window_order_keys(
    tree: Expr,
    *,
    model_keys: ModelKeys,
    scope_index: ScopeIndex | None = None,
) -> tuple[Finding, ...]:
    """Flag window ORDER BYs whose partition+order keys are not a unique tuple.

    A scope is checkable when its FROM resolves to a single relation with known
    keys (a ref'd model or an in-scope CTE) and there are no joins. Multi-source
    scopes need column-level lineage and stay silent.
    """
    scopes = _scope_index_for(tree, model_keys, scope_index)
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        source_keys = _single_source_keys(sel, model_keys=model_keys, scope_index=scopes)
        if source_keys is None:
            continue
        for w in sg.find_all_windows(sel):
            if not _node_in_scope(w, sel):
                continue
            order = sg.order_of(w)
            if order is None:
                continue
            uncovered = _uncovered_order_keys(order.expressions, sg.partition_of(w), source_keys)
            if uncovered is None:
                continue
            order_cols, partition_cols = uncovered
            rendered = sg.render_sql(w)
            out.append(
                Finding(
                    kind=FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS,
                    message=(
                        f"window {rendered} orders by {sorted(order_cols)} "
                        f"partitioned by {sorted(partition_cols) or '()'}, "
                        f"and no known uniqueness key on the source covers the combined "
                        f"key set. Ties in the order keys produce a non-deterministic "
                        f"ranking; add a stable tiebreaker."
                    ),
                    sql_snippet=rendered,
                    line_start=_line_start(w),
                    line_end=_line_end(w),
                )
            )
    return tuple(out)


def detect_non_unique_aggregate_order_keys(
    tree: Expr,
    *,
    model_keys: ModelKeys,
    scope_index: ScopeIndex | None = None,
) -> tuple[Finding, ...]:
    """Flag a top-n ordered aggregate whose order key is not unique within its group.

    ``ARRAY_AGG(x ORDER BY k LIMIT n)`` (and ``STRING_AGG``/``GROUP_CONCAT`` likewise) keeps
    only the first ``n`` elements, so *which* elements survive is deterministic only when the
    order key totally orders the rows of each group. With ties at the cutoff the ``LIMIT`` keeps
    an arbitrary winner, so the result drifts run to run even though an ``ORDER BY`` is present.
    This is the aggregate analog of :func:`detect_non_unique_window_order_keys`: a ``GROUP BY``
    plays the partition's role, and the combined (group, order) key set must be covered by a
    known uniqueness key of the single source. An aggregate with no ``GROUP BY`` folds the whole
    relation, so the order key alone must be unique.

    The hazard is reproducibility, not correctness, which is why this is a ``warn`` (see
    :func:`dblect.severity._structural_severity`): a top-n by ``k`` is a genuine top-n by ``k``,
    every surviving element legitimately among the highest-ranked, so no row is *wrong*. What is
    not pinned is which tied element the cutoff keeps. That still bites downstream: a metric
    folded over the selected set (the average basket size of the ten most expensive orders, say)
    is correct under whichever tie-break happened, yet drifts across runs. A stable tiebreaker
    removes the drift.

    Only the top-n shape fires: an ordered aggregate with no inner ``LIMIT`` keeps every element,
    so its membership is deterministic regardless of ties (only the internal tie order is
    unstable, which the unordered-aggregate detector's territory). An aggregate with no
    ``ORDER BY`` at all is that detector's job too, and stays silent here.

    Conservative toward silence, like the window check: single-source scopes only (a join or
    ``UNION`` needs column-level lineage), bare-column order and group keys only (an expression
    needs an equivalence we do not model), and silent when no source key is known (the firewall
    posture, with no grain to name there is no positive fact to fire on).
    """
    scopes = _scope_index_for(tree, model_keys, scope_index)
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        source_keys = _single_source_keys(sel, model_keys=model_keys, scope_index=scopes)
        if source_keys is None:
            continue
        group = sg.group_of(sel)
        grouping = group.expressions if group is not None else []
        for agg in sg.find_all_ordered_aggregates(sel):
            if not _node_in_scope(agg, sel):
                continue
            order = sg.aggregate_order_of(agg)
            agg_limit = sg.aggregate_limit_of(agg)
            if order is None or agg_limit is None or sg.limit_keeps_no_rows(agg_limit):
                continue
            uncovered = _uncovered_order_keys(order.expressions, grouping, source_keys)
            if uncovered is None:
                continue
            order_cols, group_cols = uncovered
            rendered = sg.render_sql(agg)
            out.append(
                Finding(
                    kind=FindingKind.NON_UNIQUE_AGGREGATE_ORDER_KEYS,
                    message=(
                        f"top-n aggregate {rendered} orders by {sorted(order_cols)} "
                        f"grouped by {sorted(group_cols) or '()'}, and no known uniqueness key "
                        f"on the source covers the combined key set. The LIMIT keeps an arbitrary "
                        f"winner among rows that tie on the order keys, so which elements survive "
                        f"can drift across runs; add a stable tiebreaker."
                    ),
                    sql_snippet=rendered,
                    line_start=_line_start(agg),
                    line_end=_line_end(agg),
                )
            )
    return tuple(out)


def detect_join_fanout(
    tree: Expr,
    *,
    model_keys: ModelKeys,
    scope_index: ScopeIndex | None = None,
    duplicate_safe_builtins: frozenset[str] = frozenset(),
    target_fds: Mapping[str, FDSet] = {},
) -> tuple[Finding, ...]:
    """Flag JOINs whose joined-in side has keys that don't cover the join.

    For each JOIN whose joined-in side resolves to keys (an in-scope CTE or a
    ref'd model), we ask whether the join's equality columns cover a known key. If yes,
    the join cannot multiply rows. If no, we flag.

    Coverage is closure-based, not raw containment: a known key ``K`` is covered when the
    join columns *functionally determine* every column of ``K`` under the joined-in side's
    ``target_fds``. Where a relation has no FDs (``NO_FDS``), the closure test reduces to ``K``
    being a subset of the join columns. The generalization removes a false positive on a
    non-minimal key: a key carrying descriptive columns dependent on an id
    (``(month, platform, project_family, wiki_id, wiki_name)`` with ``wiki_id`` determining
    ``project_family`` and ``wiki_name``) is covered by a join on ``(month, platform,
    wiki_id)``, since the closure of the join columns reaches the rest.

    The finding is suppressed when the fan-out is collapsed in the same query before
    any duplicate-sensitive consumer reads the multiplied rows: a ``GROUP BY`` over a
    scope whose every aggregate is duplicate-safe (``max``, ``min``, ``any_value``, the
    boolean folds, a ``DISTINCT`` aggregate). The row multiplication cannot then change
    any output value, so the structurally-real fan-out is not an output hazard (issue
    #170). A ``sum``/``count``/``avg`` over the joined rows, or a raw passthrough with no
    grouping, keeps it firing. ``duplicate_safe_builtins`` lets the adapter name UDF
    aggregates the duplicate-sensitivity predicate would otherwise treat as sensitive.

    Silent when the joined-in side has no known keys, when the ON predicate is not
    a conjunction of equalities between bare columns with exactly one side on the
    joined-in alias, on a ``CROSS`` join (an explicit cartesian product), or on a join
    that filters rather than multiplies the probe rows (a SEMI or ANTI join, and the
    ``LEFT JOIN ... IS NULL`` anti-join idiom).
    """
    scopes = _scope_index_for(tree, model_keys, scope_index)
    cte_bodies: Mapping[str, Expr] = {
        cte.alias_or_name: cte.this for cte in tree.find_all(exp.CTE) if isinstance(cte.this, Expr)
    }
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        # A SEMI/ANTI join, and the LEFT JOIN ... IS NULL anti-join idiom, filter the probe
        # rows rather than multiply them, so they can no more fan out than a CROSS join can be
        # covered; skip them the same way.
        anti_arms = anti_join.anti_arm_ids(sel)
        for j in sg.joins_of(sel):
            if sg.join_side_of(j) in (JoinSide.CROSS, JoinSide.SEMI, JoinSide.ANTI):
                continue
            if id(j) in anti_arms:
                continue
            target = j.this
            if not isinstance(target, exp.Table):
                continue
            target_keys = _resolve_target_keys(
                target.name, cte_bodies=cte_bodies, scope_index=scopes, model_keys=model_keys
            )
            if not target_keys:
                continue
            on = sg.on_of(j)
            if on is None:
                continue
            joined_cols = sg.equality_cols_on_alias(on, target.alias_or_name)
            if not joined_cols:
                continue
            # ``target_keys`` resolves a query-local CTE ahead of a same-named model, but
            # ``target_fds`` only carries manifest relations. So for a CTE that shadows a model
            # we must not read the model's FDs (they describe a different relation); fall back to
            # NO_FDS, i.e. plain containment, which is always sound. A CTE genuinely unique on a
            # subset already surfaces that subset as one of its structural keys.
            fds = NO_FDS if target.name in cte_bodies else target_fds.get(target.name, NO_FDS)
            if any(all(determines(fds, joined_cols, col) for col in k) for k in target_keys):
                continue
            if _collapsed_before_sensitive_consumer(sel, safe_builtins=duplicate_safe_builtins):
                continue
            sample_keys = ", ".join(sorted(joined_cols))
            known_keys = "; ".join("(" + ", ".join(sorted(k)) + ")" for k in target_keys)
            out.append(
                Finding(
                    kind=FindingKind.JOIN_FANOUT,
                    message=(
                        f"JOIN to {target.name} on ({sample_keys}) isn't covered by any "
                        f"known uniqueness key on {target.name} (known: {known_keys}); "
                        f"the join can multiply rows. Either pin the join to a unique key "
                        f"or aggregate the joined-in side first."
                    ),
                    sql_snippet=sg.render_sql(j),
                    line_start=_line_start(j),
                    line_end=_line_end(j),
                )
            )
    return tuple(out)


def detect_limit_without_deterministic_order(
    tree: Expr,
    *,
    model_keys: ModelKeys,
    scope_index: ScopeIndex | None = None,
    is_materialized: bool,
) -> tuple[Finding, ...]:
    """Flag a persisted model whose top scope ``LIMIT``s without a total ordering.

    A ``LIMIT n`` keeps an arbitrary slice unless the rows are totally ordered first, so a
    re-run can materialize a different set of rows. This is the ``LIMIT`` analog of
    :func:`detect_non_unique_window_order_keys`: the same uniqueness keys decide whether an
    ``ORDER BY`` is total. Two shapes fire:

    * No ``ORDER BY`` at all. The slice is arbitrary on its face, so this fires without
      grounding (no source key is needed to know the rows are unpinned).
    * An ``ORDER BY`` whose keys are not covered by any known uniqueness key of the source.
      Ties at the cutoff are broken arbitrarily, so which rows survive drifts.

    ``is_materialized`` gates the whole check: a view (or ephemeral model) recomputes the
    ``LIMIT`` per query, so the determinism question is the consumer's and the caller passes
    ``False``. Only a persisted materialization (``table``, ``incremental``,
    ``materialized_view``) stores the sampled rows.

    Conservative toward silence: it reasons only about the top scope (an inner-scope
    ``LIMIT`` in a CTE or subquery is left for later), only about a single-source top scope
    the uniqueness machinery can ground (a join or ``UNION`` top scope stays silent), and
    only about bare-column order keys (an ``ORDER BY`` over an expression needs an
    equivalence check we do not model). When an ``ORDER BY`` is present but no source key is
    known, it stays silent rather than guess the ordering is non-unique. A top scope that
    yields a single row (an ungrouped aggregate) is exempt: SQL's implicit grouping collapses
    it to one row, so a ``LIMIT`` cannot drop a row. Order keys are resolved through the
    projection's aliases before being matched, so an ``order by <alias>`` of a key counts as
    covering (and a renamed non-key column does not pass as the key).
    """
    if not is_materialized or not isinstance(tree, exp.Select):
        return ()
    limit = tree.args.get("limit")
    if not isinstance(limit, exp.Limit):
        return ()
    if sg.limit_keeps_no_rows(limit):
        return ()
    if _is_single_row_scope(tree):
        return ()
    order = tree.args.get("order")
    if not isinstance(order, exp.Order) or not order.expressions:
        return (_limit_finding(limit, ordered=False),)
    source_keys = _single_source_keys(
        tree, model_keys=model_keys, scope_index=_scope_index_for(tree, model_keys, scope_index)
    )
    if source_keys is None:
        return ()
    order_cols = _bare_column_names(list(sg.statement_order_targets(tree)))
    if order_cols is None:
        return ()
    projection = _projection_aliases(tree)
    covered = frozenset(projection.get(c, c) for c in order_cols)
    if any(k <= covered for k in source_keys):
        return ()
    return (_limit_finding(limit, ordered=True, order_cols=order_cols),)


def _is_persisted_materialization(materialized: str | None) -> bool:
    """True when the materialization stores its rows, so a non-deterministic ``LIMIT`` freezes
    an arbitrary slice. Decided exhaustively over the closed materialization vocabulary so a
    new kind is a type error here rather than a silent fall-through: a view or ephemeral model
    recomputes the query per read (its ``LIMIT`` is the consumer's determinism question), and
    an adapter-specific or absent materialization is treated as non-persisted (the firewall
    posture: fire only on a positively persisted materialization). A snapshot persists an SCD-2
    table, so it counts as persisted; snapshot trees do not reach this detector today (the
    audit walker scans ``manifest.models`` only), but the classification stays truthful for any
    future consumer."""
    kind = Materialization.from_raw(materialized)
    match kind:
        case (
            Materialization.TABLE
            | Materialization.INCREMENTAL
            | Materialization.MATERIALIZED_VIEW
            | Materialization.SNAPSHOT
        ):
            return True
        case Materialization.VIEW | Materialization.EPHEMERAL | Materialization.OTHER:
            return False
    assert_never(kind)


def _limit_finding(
    limit: exp.Limit, *, ordered: bool, order_cols: list[str] | None = None
) -> Finding:
    """Build the LIMIT finding, located at the ``LIMIT`` clause. ``ordered`` picks the
    message: a present-but-non-unique ORDER BY versus no ORDER BY at all."""
    if ordered:
        detail = (
            f"its `ORDER BY {sorted(order_cols or [])}` is not covered by any known "
            "uniqueness key on the source, so ties at the cutoff are broken arbitrarily and "
            "which rows survive can drift across runs"
        )
    else:
        detail = (
            "it has no `ORDER BY`, so it materializes an arbitrary sample of rows that can "
            "differ across runs"
        )
    return Finding(
        kind=FindingKind.LIMIT_WITHOUT_DETERMINISTIC_ORDER,
        message=(
            f"top-level `LIMIT` in a persisted model is not deterministic: {detail}. "
            "Order by a key that uniquely identifies a row (add a tiebreaker), or drop the "
            f"`LIMIT`. {suppression_hint(FindingKind.LIMIT_WITHOUT_DETERMINISTIC_ORDER)}"
        ),
        sql_snippet=sg.render_sql(limit),
        line_start=_line_start(limit),
        line_end=_line_end(limit),
    )


def _is_single_row_scope(sel: exp.Select) -> bool:
    """True when ``sel`` is an ungrouped aggregate, so it yields exactly one row.

    SQL's implicit grouping collapses a SELECT with a collapsing aggregate in its projection
    or HAVING and no GROUP BY to a single row, so a ``LIMIT`` cannot drop a row and the slice
    is deterministic. A windowed aggregate (``count(*) over ()``) preserves rows and does not
    establish the shape; an adapter-unknown UDF might not aggregate at all, so neither does it
    (the check stays conservative and lets the ``LIMIT`` fire). A GROUP BY produces one row per
    group, so it is not single-row.
    """
    if sg.group_of(sel) is not None:
        return False
    consumers: list[Expr] = list(sel.expressions)
    having = sel.args.get("having")
    if isinstance(having, exp.Having) and isinstance(having.this, Expr):
        consumers.append(having.this)
    for root in consumers:
        for node in root.walk():
            if (
                isinstance(node, exp.AggFunc)
                and node.find_ancestor(exp.Select) is sel
                and not _within_window(node, sel)
            ):
                return True
    return False


def _within_window(node: Expr, sel: exp.Select) -> bool:
    """True when ``node`` sits inside an ``OVER`` window belonging to ``sel``: a windowed
    aggregate preserves rows, unlike a collapsing one."""
    cur = node.parent
    while cur is not None and cur is not sel:
        if isinstance(cur, exp.Window):
            return True
        cur = cur.parent
    return False


def _projection_aliases(sel: exp.Select) -> dict[str, str]:
    """Map each output name in ``sel``'s projection that renames a bare column to that source
    column. ``ORDER BY`` resolves a bare name to a SELECT-list alias, so translating order keys
    through this map lets an ``order by <alias>`` be matched against the source's uniqueness
    keys, and stops a column renamed to a key's name from passing as that key. An alias over an
    expression has no single source column and is omitted."""
    out: dict[str, str] = {}
    for proj in sel.expressions:
        if isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Column):
            out[proj.alias_or_name] = sg.column_name(proj.this)
    return out


# The relation graph and the uniqueness annotations propagated over it. The fact-grounded
# and cross-model fan-out factories both rest on this pair, so an audit computes it once and
# threads it into both rather than re-running the fixpoint per factory.
RelationUniqueness = tuple[RelationLineageGraph, Mapping[SourceRef, Annotation[CandidateKeySet]]]


def relation_uniqueness(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
    graph: RelationLineageGraph | None = None,
) -> RelationUniqueness:
    """Build the relation graph and propagate the uniqueness property over it.

    ``propagate`` memoizes only within a single call, so the two detector factories that need
    this pair would otherwise each rebuild the graph and re-run the whole-manifest uniqueness
    fixpoint. :func:`dblect.audit.walker.run_audit` computes it once and passes it to both.
    ``parsed`` shares the audit's already-parsed trees; ``graph`` shares a relation graph the
    check family already built (``analyze`` threads it) so the build runs once per run, while the
    uniqueness fixpoint still runs here (the two families propagate different properties).
    """
    if graph is None:
        graph = build_relation_graph(manifest, dialect=profile.sqlglot_dialect, parsed=parsed).graph
    keys = propagate(graph, uniqueness_property(manifest, profile, parsed=parsed))
    return graph, keys


def fd_annotations_by_name(
    manifest: Manifest,
    graph: RelationLineageGraph,
    fd_facts: tuple[Fact[FDSet, SourceRef], ...] = (),
) -> dict[str, FDSet]:
    """Propagate the functional-dependency property over the relation graph, indexed by the
    relation name as it appears in compiled SQL.

    Grounded from the declared ``determines`` facts the caller threads in; even with none, the
    property still derives structural FDs (a GROUP BY key, a join's ON equalities), sound on
    their own. Both the join-fanout detector (key coverage through ``determines``) and the
    join-on-nullable-key detector (folding a co-determined key column into its declared key)
    read this map, so :func:`dblect.audit.walker.run_audit` computes it once over the shared
    graph and threads it into both factories rather than re-running the fixpoint per factory."""
    fd_prop = functional_dependency_property(functional_dependency_grounding(by_scope(fd_facts)))
    return index_by_name(
        manifest, {ref: ann.value for ref, ann in propagate(graph, fd_prop).items()}
    )


def make_fact_grounded_detectors(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
    relation_keys: RelationUniqueness | None = None,
    fd_facts: tuple[Fact[FDSet, SourceRef], ...] = (),
    fd_by_name: Mapping[str, FDSet] | None = None,
) -> tuple[Detector, ...]:
    """Curry the fact-grounded detectors against substrate-derived keys.

    Per-model keys come from one cross-model propagation of the uniqueness
    property over the relation graph; ``parsed`` lets the caller share the audit's
    already-parsed trees so the graph build does not re-parse. Each curried
    detector consults a per-tree scope index, cached so the relation walk runs at
    most once per tree no matter how many detectors consume it.

    ``profile`` is the run's resolved target: its dialect parses the graph and its
    semantics ground the uniqueness keys, so parsing and enforcement agree.
    ``relation_keys`` lets the audit pass an already-propagated graph/keys pair (see
    :func:`relation_uniqueness`) so the fixpoint is not re-run.

    The LIMIT-without-deterministic-order detector also needs each tree's resolved
    materialization (it exempts views), which the bare tree does not carry. It is read
    from ``parsed`` here and addressed by ``id(tree)``, the same per-tree addressing the
    scope-index cache uses; a caller that omits ``parsed`` leaves that detector silent
    (no tree-to-materialization map to consult) while the key-grounded pair still works.
    """
    materialized_by_tree: dict[int, bool] = {}
    for uid, tree in (parsed or {}).items():
        node = manifest.models.get(uid)
        config = node.config if node is not None else None
        materialized_by_tree[id(tree)] = _is_persisted_materialization(
            config.materialized if config is not None else None
        )
    graph, keys = (
        relation_keys
        if relation_keys is not None
        else relation_uniqueness(manifest, profile, parsed=parsed)
    )
    # Predicate-flow is consulted only where a conditional key waits to activate, so
    # seed the flow pass with those scopes and let it pull in their upstreams rather
    # than walking every relation in the graph. The seed must stay exactly "every
    # relation carrying a conditional key": intra-model activation reads flow at every
    # such relation (via ``flow_by_name`` below), so narrowing the seed (e.g. to
    # conditional *owners* only) would silently stop carriers from activating. It is
    # the same set ``conditional_by_name`` indexes, both derived from ``keys``.
    conditional_scopes = [ref for ref, ann in keys.items() if ann.value.conditional]
    flow = propagate(graph, predicate_flow_property(), subjects=conditional_scopes)
    activated = activate_conditional(keys, flow)
    model_keys = index_by_name(manifest, {ref: cks.keys for ref, cks in activated.items()})
    conditional_by_name = index_by_name(
        manifest, {ref: ann.value.conditional for ref, ann in keys.items()}
    )
    flow_by_name = index_by_name(manifest, {ref: ann.value for ref, ann in flow.items()})
    # The functional-dependency map lets join-fanout test key coverage through ``determines`` (a
    # join covering a key's determinant covers the key). ``run_audit`` propagates it once over the
    # shared graph and threads it in; a standalone caller lets it default and we propagate here.
    if fd_by_name is None:
        fd_by_name = fd_annotations_by_name(manifest, graph, fd_facts)
    cache: dict[int, ScopeIndex] = {}

    def scope_index(tree: Expr) -> ScopeIndex:
        hit = cache.get(id(tree))
        if hit is None:
            scope_flow = relation_scope_filters(tree, flow_by_name)
            hit = activated_scope_keys(tree, model_keys, conditional_by_name, scope_flow)
            cache[id(tree)] = hit
        return hit

    def window_keys(tree: Expr) -> tuple[Finding, ...]:
        return detect_non_unique_window_order_keys(
            tree, model_keys=model_keys, scope_index=scope_index(tree)
        )

    def aggregate_order_keys(tree: Expr) -> tuple[Finding, ...]:
        return detect_non_unique_aggregate_order_keys(
            tree, model_keys=model_keys, scope_index=scope_index(tree)
        )

    def fanout(tree: Expr) -> tuple[Finding, ...]:
        return detect_join_fanout(
            tree,
            model_keys=model_keys,
            scope_index=scope_index(tree),
            target_fds=fd_by_name,
            duplicate_safe_builtins=profile.duplicate_safe_aggregate_builtins,
        )

    def limit_order(tree: Expr) -> tuple[Finding, ...]:
        return detect_limit_without_deterministic_order(
            tree,
            model_keys=model_keys,
            scope_index=scope_index(tree),
            is_materialized=materialized_by_tree.get(id(tree), False),
        )

    return (window_keys, fanout, limit_order, aggregate_order_keys)


# Per-relation views the cross-model fan-out detector reads, all keyed by the relation's
# ``SourceRef`` so an origin recovered from a provenance ``ColumnRef`` needs no name lookup.
KeysBySource = Mapping[SourceRef, CandidateKeySet]
ProvenanceBySource = Mapping[SourceRef, Mapping[str, frozenset[ColumnRef]]]
NameToRef = Mapping[str, SourceRef]


def detect_cross_model_fanout(
    tree: Expr,
    *,
    name_to_ref: NameToRef,
    keys_by_source: KeysBySource,
    provenance_by_source: ProvenanceBySource,
    duplicate_safe_builtins: frozenset[str] = frozenset(),
) -> tuple[Finding, ...]:
    """Flag a duplicate-sensitive aggregate that folds a magnitude an upstream fan-out
    replicated, over a relation no longer keyed at the magnitude's grain.

    The local ``detect_join_fanout`` fires at the join that multiplies rows; it cannot see a
    downstream model that then sums a replicated magnitude. Here, for a single-source SELECT
    whose FROM is a ref'd relation ``R``, each duplicate-sensitive aggregate's argument
    columns trace (via ``R``'s where-provenance) back to their origin sources. The magnitude
    is single-counted only when ``R`` is still unique at the grain of the origin it came
    from: ``grain_preserved`` over ``R``'s propagated uniqueness, with the origin's key
    translated into ``R``'s column names through provenance. When no origin candidate key
    survives in ``R``, the fold can double count and we flag.

    A grain-collapse guard precedes the per-aggregate check: when the relation is provably
    unique at the GROUP BY grain (a candidate key fits within the grouping columns), every
    bucket is a single row and no fold over it can over-count, so the whole select is silent.
    This is the cross-model analog of the local ``_collapsed_before_sensitive_consumer`` guard,
    and it clears the magnitude path's grouped-to-a-finer-grain case (``SUM(amount) GROUP BY
    order_id, item_id`` over line-grain staging) as well.

    Silent when the FROM is not a single ref'd relation (a join or a CTE/subquery needs
    column-level reasoning kept for later), when the aggregate is duplicate-safe, and when the
    origin relation has no known key, the firewall posture: with no grain to name there is no
    positive fact to fire on. Also silent on a COUNT-behavior fold (``COUNT(*)``, ``COUNT(1)``,
    ``COUNT(col)``, ``COUNT_IF``): it yields a cardinality, not a magnitude, so it counts the
    relation's rows (whose grain the relation preserves) rather than summing a replicated value.
    That makes every COUNT the ``SUM(qty)`` analog (a fold at the genuine, un-replicated grain),
    not the ``SUM(amount)`` analog: a count per group reads distinct rows, so a single-level
    fan-out does not make it double count. The fan-trap case where it would (independent
    fan-outs leaving the relation with no key) is indistinguishable from an undeclared grain, so
    the firewall keeps it silent there too (issue #179).
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        ref = _single_from_ref(sel, name_to_ref)
        if ref is None:
            continue
        rel_prov = provenance_by_source.get(ref, {})
        rel_keys = keys_by_source.get(ref, NO_KEYS)
        group_cols = _group_by_columns(sel)
        # Grain-collapse guard: when the relation is provably unique at the GROUP BY grain
        # (a candidate key fits within the grouping columns), every bucket is a single row, so
        # no fold over it can over-count, whatever magnitude it reads. This is the cross-model
        # analog of the local ``_collapsed_before_sensitive_consumer`` guard.
        if group_cols is not None and grain_preserved(rel_keys, group_cols):
            continue
        for agg in _sensitive_aggregate_consumers(sel, safe_builtins=duplicate_safe_builtins):
            origin = _replicated_origin(
                agg, rel_keys=rel_keys, rel_prov=rel_prov, keys_by_source=keys_by_source
            )
            if origin is not None:
                out.append(
                    Finding(
                        kind=FindingKind.CROSS_MODEL_FANOUT,
                        message=(
                            f"{sg.render_sql(agg)} folds a magnitude from {origin.unique_id} "
                            f"that an upstream fan-out can replicate, over a relation not keyed "
                            f"at that grain, so the result can double count. Collapse the "
                            f"fan-out to the origin grain before this aggregate (GROUP BY a key "
                            f"that covers it, or pre-aggregate the producing model)."
                        ),
                        sql_snippet=sg.render_sql(agg),
                        line_start=_line_start(agg),
                        line_end=_line_end(agg),
                    )
                )
    return tuple(out)


def make_cross_model_fanout_detectors(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
    relation_keys: RelationUniqueness | None = None,
    column_graph: ColumnLineageGraph | None = None,
) -> tuple[Detector, ...]:
    """Curry the cross-model fan-out detector against two propagated properties.

    Uniqueness comes from the relation graph (which relation is keyed at which grain) and
    where-provenance from the column graph (which source a magnitude traces to). Both are
    propagated once over the whole manifest; ``parsed`` shares the audit's already-parsed
    trees so neither graph re-parses. ``relation_keys`` lets the audit pass the
    already-propagated uniqueness (see :func:`relation_uniqueness`) so the fixpoint, also
    needed by :func:`make_fact_grounded_detectors`, is not run twice. ``column_graph``
    likewise lets the audit pass the manifest column graph it built once, so the heavy
    qualify-and-resolve walk is not repeated per fact family.
    """
    _, keys = (
        relation_keys
        if relation_keys is not None
        else relation_uniqueness(manifest, profile, parsed=parsed)
    )
    keys_by_source: dict[SourceRef, CandidateKeySet] = {ref: ann.value for ref, ann in keys.items()}

    col_graph = (
        column_graph
        if column_graph is not None
        else build_manifest_graph(manifest, dialect=profile.sqlglot_dialect, parsed=parsed).graph
    )
    provenance = propagate(col_graph, where_provenance)
    provenance_by_source = _provenance_by_source(provenance)
    name_to_ref = index_by_name(manifest, {ref: ref for ref in keys_by_source})

    def fanout(tree: Expr) -> tuple[Finding, ...]:
        return detect_cross_model_fanout(
            tree,
            name_to_ref=name_to_ref,
            keys_by_source=keys_by_source,
            provenance_by_source=provenance_by_source,
            duplicate_safe_builtins=profile.duplicate_safe_aggregate_builtins,
        )

    return (fanout,)


def _single_from_ref(sel: exp.Select, name_to_ref: NameToRef) -> SourceRef | None:
    """The ``SourceRef`` of ``sel``'s FROM when it is a single ref'd relation with no joins.

    A join or a non-table FROM (subquery) needs column-level reasoning we keep for later, and
    a name shadowed by a CTE in ``sel``'s lexical scope is a per-query scope the propagator
    does not annotate, so all three return ``None`` and the detector stays silent. CTE
    resolution uses :func:`_cte_body_for`, walking the enclosing WITH chain outward, so a name
    defined only as a CTE in an unrelated sibling scope does not shadow a genuine relation read.
    """
    if sg.joins_of(sel):
        return None
    from_ = sg.from_of(sel)
    if from_ is None or not isinstance(from_.this, exp.Table):
        return None
    name = from_.this.name
    if _cte_body_for(name, sel) is not None:
        return None
    return name_to_ref.get(name)


def _group_by_columns(sel: exp.Select) -> frozenset[str] | None:
    """The GROUP BY key column names of ``sel`` when every grouping term is a bare column,
    else ``None`` (no GROUP BY, or a positional/expression key whose grain we cannot size).

    ``None`` carries the same "cannot judge" meaning as elsewhere: it disables the
    grain-collapse guard, so an un-sizable grouping keeps the detector conservative rather than
    proving a collapse it cannot.
    """
    group = sg.group_of(sel)
    if group is None or not group.expressions:
        return None
    names = _bare_column_names(group.expressions)
    return frozenset(names) if names is not None else None


def _replicated_origin(
    agg: Expr,
    *,
    rel_keys: CandidateKeySet,
    rel_prov: Mapping[str, frozenset[ColumnRef]],
    keys_by_source: KeysBySource,
) -> SourceRef | None:
    """The origin source whose grain the aggregated relation does not preserve, or ``None``.

    The aggregate's argument columns trace to one or more origins. For each origin with a
    known key, the magnitude is single-counted only when the relation keeps a key refining
    that origin's grain (translated into the relation's columns). The first origin that fails
    is the replicated side the fold double counts; an origin with no known key is skipped
    (nothing to claim).

    A COUNT-behavior fold (``COUNT(*)``, ``COUNT(col)``, ``COUNT_IF``) yields a cardinality,
    not a magnitude: it counts rows (modulo nulls), and the row grain is what the relation
    preserves, so the replicated value a counted column carries is never summed and cannot
    double count. ``COUNT(amount)`` over a fan-out reads distinct rows, not a replicated
    magnitude, exactly like ``COUNT(*)`` and unlike ``SUM(amount)``. It returns ``None`` here.
    The fan-trap where a COUNT would over-count leaves the relation with no key at all,
    indistinguishable from an undeclared grain, so the firewall keeps it silent there too.
    """
    if isinstance(agg, exp.AggFunc) and aggregate_behavior(agg) is AggregateBehavior.COUNT:
        return None
    origin_refs: set[ColumnRef] = set()
    for c in {sg.column_name(c) for c in sg.find_columns(agg)}:
        origin_refs |= set(rel_prov.get(c, frozenset()))
    by_source: dict[SourceRef, set[str]] = {}
    for col_ref in origin_refs:
        by_source.setdefault(col_ref.source, set()).add(col_ref.column)
    for origin in sorted(by_source, key=lambda s: s.unique_id):
        origin_keys = keys_by_source.get(origin)
        if origin_keys is None or not origin_keys.keys:
            continue
        if not _origin_grain_preserved(rel_keys, rel_prov, origin, origin_keys):
            return origin
    return None


def _origin_grain_preserved(
    rel_keys: CandidateKeySet,
    rel_prov: Mapping[str, frozenset[ColumnRef]],
    origin: SourceRef,
    origin_keys: CandidateKeySet,
) -> bool:
    """True when the aggregated relation stays unique at some candidate grain of ``origin``.

    Each of the origin's candidate keys is translated into the relation's column names through
    provenance; the relation preserves the grain when a surviving key refines any translated
    origin key. A key whose columns the relation does not carry cannot witness the grain and
    is skipped.
    """
    for okey in origin_keys.keys:
        translated = _translate_key(okey, origin, rel_prov)
        if translated is not None and grain_preserved(rel_keys, translated):
            return True
    return False


def _translate_key(
    origin_key: Key, origin: SourceRef, rel_prov: Mapping[str, frozenset[ColumnRef]]
) -> Key | None:
    """``origin_key`` rewritten in the aggregated relation's column names, or ``None`` when the
    relation carries no column tracing to one of the origin key's columns (so the relation
    cannot be unique at that grain). A column is a carrier when the origin key column is in its
    where-provenance; the lexicographically first carrier is chosen when several alias it."""
    translated: set[str] = set()
    for origin_col in origin_key:
        target = ColumnRef(origin, origin_col)
        carriers = sorted(name for name, prov in rel_prov.items() if target in prov)
        if not carriers:
            return None
        translated.add(carriers[0])
    return frozenset(translated)


def _provenance_by_source(
    provenance: Mapping[ColumnRef, Annotation[frozenset[ColumnRef]]],
) -> dict[SourceRef, dict[str, frozenset[ColumnRef]]]:
    """Group the per-column where-provenance by its relation, ``column -> source columns``."""
    by_source: dict[SourceRef, dict[str, frozenset[ColumnRef]]] = {}
    for col_ref, ann in provenance.items():
        by_source.setdefault(col_ref.source, {})[col_ref.column] = ann.value
    return by_source


def _scope_index_for(
    tree: Expr, model_keys: ModelKeys, scope_index: ScopeIndex | None
) -> ScopeIndex:
    """Resolve a per-scope index, computing one if the caller didn't supply it.

    Tests call the detectors directly without precomputing the index; the audit
    walker always supplies a cached one so this branch costs nothing in production.
    """
    if scope_index is not None:
        return scope_index
    return relation_scope_keys(tree, model_keys)


def _single_source_keys(
    sel: exp.Select, *, model_keys: ModelKeys, scope_index: ScopeIndex
) -> frozenset[Key] | None:
    """Keys for ``sel``'s single FROM source, or ``None`` if it is not a clean
    single-source scope with known keys.

    A scope qualifies when there are no JOINs and FROM is a single source: a bare
    table (resolving to a CTE via the scope index, or a model ref via the per-model
    map) or an inline subquery (its keys from the scope index, which records every
    SELECT/UNION scope). Returns ``None`` when the shape doesn't qualify or no key
    is known, so the window detector stays silent.
    """
    from_ = sg.from_of(sel)
    if from_ is None or sg.joins_of(sel):
        return None
    target = from_.this
    if isinstance(target, exp.Table):
        cte_body = _cte_body_for(target.name, sel)
        if cte_body is not None:
            keys = scope_index.get(id(cte_body), frozenset())
            return keys or None
        keys = model_keys.get(target.name, frozenset())
        return keys or None
    if isinstance(target, exp.Subquery) and isinstance(target.this, Expr):
        keys = scope_index.get(id(target.this), frozenset())
        return keys or None
    return None


def _resolve_target_keys(
    name: str,
    *,
    cte_bodies: Mapping[str, Expr],
    scope_index: ScopeIndex,
    model_keys: ModelKeys,
) -> frozenset[Key]:
    """Keys for ``name``, looked up as an in-scope CTE first, then as a model ref.

    An empty result means "no known keys", which the join-fanout detector reads as
    "stay silent". A local CTE shadows a model of the same name, matching SQL's
    resolution rules.
    """
    body = cte_bodies.get(name)
    if body is not None:
        return scope_index.get(id(body), frozenset())
    return model_keys.get(name, frozenset())


def _cte_body_for(name: str, sel: exp.Select) -> Expr | None:
    """The CTE body matching ``name`` in ``sel``'s enclosing WITH, walking outward
    to honour lexical CTE scoping."""
    node: Expr | None = sel
    while node is not None:
        if isinstance(node, exp.Select):
            w = node.args.get("with_")
            if isinstance(w, exp.With):
                for cte in w.expressions:
                    if isinstance(cte, exp.CTE) and cte.alias_or_name == name:
                        body = cte.this
                        return body if isinstance(body, Expr) else None
        node = node.parent
    return None


def _collapsed_before_sensitive_consumer(sel: exp.Select, *, safe_builtins: frozenset[str]) -> bool:
    """True when ``sel``'s GROUP BY collapses any fan-out before a duplicate-sensitive
    consumer reads the multiplied rows.

    After a GROUP BY the output is one row per group, so a row multiplication changes an
    output value only through a duplicate-sensitive aggregate (``sum``, ``count``,
    ``array_agg``: it folds the duplicated rows). The collapse clears only when every
    aggregate consumer of the grouped rows (a projection or a HAVING term) is positively
    known to be duplicate-safe: an idempotent fold, a ``DISTINCT`` aggregate, or a UDF the
    adapter named in ``safe_builtins``. An aggregate UDF sqlglot leaves as ``exp.Anonymous``
    is sensitive by default, so an unknown fold keeps the finding firing. A scope with no
    GROUP BY (the fan-out flows straight to the rows) is never collapsed here. The collapse
    clears exactly when ``sel`` has no duplicate-sensitive consumer (see
    :func:`_sensitive_aggregate_consumers` for which nodes count).
    """
    if sg.group_of(sel) is None:
        return False
    return next(_sensitive_aggregate_consumers(sel, safe_builtins=safe_builtins), None) is None


def _sensitive_aggregate_consumers(
    sel: exp.Select, *, safe_builtins: frozenset[str]
) -> Iterator[Expr]:
    """The duplicate-sensitive aggregate consumers of ``sel``'s rows: a typed aggregate or an
    adapter-unknown UDF, sitting in a projection or HAVING term, that belongs to ``sel`` and
    is not a grouped scalar projection.

    Two scoping rules keep the set honest. A node is read only when it belongs to ``sel``
    itself, never a nested sub-SELECT, whose aggregate folds different rows. And an
    ``exp.Anonymous`` call is weighed only when it reads a column outside the grouping keys: a
    function over grouping keys alone is a grouped scalar projection (valid SQL guarantees
    grouped non-aggregates), constant within a group, so a fan-out that duplicates rows cannot
    move it. A typed aggregate is always weighed, since even ``sum`` of a grouping key scales
    with the duplicated row count.
    """
    group_keys = _group_key_columns(sel)
    consumers: list[Expr] = list(sel.expressions)
    having = sel.args.get("having")
    if isinstance(having, exp.Having) and isinstance(having.this, Expr):
        consumers.append(having.this)
    for root in consumers:
        for node in root.walk():
            if not isinstance(node, exp.AggFunc | exp.Anonymous):
                continue
            if node.find_ancestor(exp.Select) is not sel:
                continue
            if isinstance(node, exp.Anonymous) and not _reads_outside_grouping(node, group_keys):
                continue
            if duplicate_sensitive(node, safe_builtins=safe_builtins):
                yield node


def _group_key_columns(sel: exp.Select) -> frozenset[tuple[str | None, str]]:
    """The ``(qualifier, name)`` of every column in ``sel``'s GROUP BY keys."""
    group = sg.group_of(sel)
    if group is None:
        return frozenset()
    return frozenset(
        (sg.column_table(c), sg.column_name(c))
        for e in group.expressions
        for c in sg.find_columns(e)
    )


def _reads_outside_grouping(node: Expr, group_keys: frozenset[tuple[str | None, str]]) -> bool:
    """True if ``node`` references a column that is not a grouping key. A call reading only
    grouping keys (or no column at all) cannot fold multiplied rows: its value is fixed
    within a group."""
    return any(
        (sg.column_table(c), sg.column_name(c)) not in group_keys for c in sg.find_columns(node)
    )


def _node_in_scope(node: Expr, sel: exp.Select) -> bool:
    """True when ``node``'s nearest enclosing SELECT is ``sel`` (not a nested sub-SELECT).

    Both the window and top-n-aggregate order-key checks read a node against ``sel``'s source
    keys and grouping, so a window or aggregate that actually belongs to a nested SELECT must be
    excluded: its keys and grouping are a different scope's."""
    cur: Expr | None = node.parent
    while cur is not None:
        if isinstance(cur, exp.Select):
            return cur is sel
        cur = cur.parent
    return False


def _uncovered_order_keys(
    order: list[Expr], grouping: list[Expr], source_keys: frozenset[Key]
) -> tuple[list[str], list[str]] | None:
    """The bare order and grouping column names when their combined key set is not covered by a
    known source key, signalling a non-total order; ``None`` when the order is provably total or
    we cannot judge it.

    The window and top-n-aggregate checks share this decision: the order is total iff some
    candidate key fits within the (grouping + order) column set. ``None`` folds the three silent
    cases both share: an empty order, an order or grouping key that is not a bare column (an
    expression we do not model an equivalence for), or a combined set a known key already covers.
    """
    if not order:
        return None
    order_cols = _bare_column_names(order)
    grouping_cols = _bare_column_names(grouping)
    if order_cols is None or grouping_cols is None:
        return None
    key_set = frozenset(order_cols) | frozenset(grouping_cols)
    if any(k <= key_set for k in source_keys):
        return None
    return order_cols, grouping_cols


def _bare_column_names(expressions: list[Expr]) -> list[str] | None:
    """Column names if every expression is a bare ``exp.Column``; else ``None``."""
    names: list[str] = []
    for e in expressions:
        target = e
        if isinstance(target, exp.Ordered):
            target = target.this
        if not isinstance(target, exp.Column):
            return None
        names.append(sg.column_name(target))
    return names


def _line_start(node: Expr) -> int:
    span = sg.line_range(node)
    return span[0] if span is not None else 0


def _line_end(node: Expr) -> int:
    span = sg.line_range(node)
    return span[1] if span is not None else 0
