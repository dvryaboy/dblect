"""Fact-grounded audit detectors that consume propagated lineage properties.

Opportunistic detectors (they fire only when the project gives enough information
to make a claim, and stay silent otherwise):

* ``detect_non_unique_window_order_keys``: window functions whose combined
  (partition, order) columns are not covered by any candidate key of the scope's
  single source. Ties in the ordering produce non-deterministic rankings.
* ``detect_non_unique_aggregate_order_keys``: the aggregate twin of the window check.
  A top-n ordered aggregate (``ARRAY_AGG(x ORDER BY k LIMIT n)``) whose (group, order)
  columns are not covered by a source key keeps an arbitrary winner among ties.
* ``detect_join_fanout``: JOINs that repeat a side's rows (the ON columns cover no known key
  of the other side) where a duplicate-sensitive consumer reads the repeated side.
* ``detect_limit_without_deterministic_order``: a persisted model whose top-scope
  ``LIMIT`` has no ``ORDER BY``, or one whose order keys are not covered by a known
  uniqueness key, so a re-run materializes a different slice of rows.
* ``detect_cross_model_fanout``: a duplicate-sensitive aggregate that folds a
  magnitude an upstream fan-out replicated, over a relation no longer keyed at the
  magnitude's grain. This one also reads ``where_provenance`` to find the origin a
  magnitude traces to, then asks ``grain_preserved`` whether that origin's grain
  still holds. A COUNT fold (``COUNT(*)``, ``COUNT(col)``) yields a cardinality, not
  a magnitude: it counts the relation's rows, whose grain the relation preserves, so
  it stays silent (the ``SUM(qty)`` analog), unlike ``SUM(amount)``.

Per-model keys and dependencies come from cross-model propagation over the
relation graph; a per-tree scope index (``relation_scope_facts``) then supplies
the facts of every FROM/JOIN source, CTE, subquery, or model alike.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import assert_never

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.lineage.builder import build_manifest_graph, build_relation_graph, index_by_name
from dblect.lineage.facts.model import Annotation, Fact, by_scope
from dblect.lineage.facts.property import Property
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import (
    ColumnLineageGraph,
    ColumnRef,
    RelationLineageGraph,
    SourceKind,
    SourceRef,
)
from dblect.lineage.properties import where_provenance
from dblect.lineage.properties.functional_dependency import (
    FDSet,
    covers,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.properties.predicate_flow import (
    predicate_flow_property,
    relation_scope_filters,
)
from dblect.lineage.properties.scope_closure import Input, JoinChain
from dblect.lineage.properties.uniqueness import (
    NO_KEYS,
    CandidateKeySet,
    Key,
    activate_conditional,
    declared_grain_keys,
    grain_preserved,
    relation_scope_facts,
    uniqueness_facts,
    uniqueness_property_from_facts,
)
from dblect.lineage.property import propagate, value_origin
from dblect.manifest import Manifest, Materialization
from dblect.sql import (
    AggregateBehavior,
    Finding,
    FindingKind,
    aggregate_behavior,
    duplicate_sensitive,
    suppression_hint,
)
from dblect.sql import _sqlglot as sg

Detector = Callable[[Expr], tuple[Finding, ...]]

# Per-model (and per-source) candidate keys, addressed by relation name as it
# appears in SQL. Per-scope facts are addressed by ``id(node)`` for the lifetime
# of one parsed tree.
ModelKeys = Mapping[str, frozenset[Key]]
ScopeIndex = Mapping[int, Input]


def detect_non_unique_window_order_keys(
    tree: Expr,
    *,
    model_keys: ModelKeys,
    model_fds: Mapping[str, FDSet] = {},
    scope_index: ScopeIndex | None = None,
) -> tuple[Finding, ...]:
    """Flag window ORDER BYs whose partition+order keys are not a unique tuple.

    A scope is checkable when its FROM resolves to a single relation with known
    keys (a ref'd model or an in-scope CTE) and there are no joins. Multi-source
    scopes need column-level lineage and stay silent.
    """
    scopes = _scope_index_for(tree, model_keys, model_fds, scope_index)
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        source = _single_source(sel, scopes)
        if source is None:
            continue
        for w in sg.find_all_windows(sel):
            if not _node_in_scope(w, sel):
                continue
            order = sg.order_of(w)
            if order is None:
                continue
            uncovered = _uncovered_order_keys(order.expressions, sg.partition_of(w), source)
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
    model_fds: Mapping[str, FDSet] = {},
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
    unstable, which is the unordered-aggregate detector's territory). An aggregate with no
    ``ORDER BY`` at all is that detector's job too, and stays silent here.

    Conservative toward silence, like the window check: single-source scopes only (a join or
    ``UNION`` needs column-level lineage), bare-column order and group keys only (an expression
    needs an equivalence we do not model), and silent when no source key is known (the firewall
    posture: with no grain to name, there is no positive fact to fire on).
    """
    scopes = _scope_index_for(tree, model_keys, model_fds, scope_index)
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        source = _single_source(sel, scopes)
        if source is None:
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
            uncovered = _uncovered_order_keys(order.expressions, grouping, source)
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
    model_fds: Mapping[str, FDSet] = {},
    scope_index: ScopeIndex | None = None,
    duplicate_safe_builtins: frozenset[str] = frozenset(),
    declared_keys: frozenset[Key] = frozenset(),
) -> tuple[Finding, ...]:
    """Flag JOINs that repeat the rows of a side a duplicate-sensitive consumer reads.

    A join repeats a row of side A when the ON columns cover no known key of side B (closure
    under B's dependencies), so it can match two B rows. The joined-in side being uncovered
    repeats the probe side (everything to its left); the probe being uncovered repeats the
    joined-in side. A claim needs a known key on the side that would do the repeating. INNER,
    LEFT, RIGHT and FULL decide alike, so ``a JOIN b`` and ``b JOIN a`` agree. An ON spanning
    several left sides leaves the probe undecided, and then only an uncovered joined-in side is
    blamed.

    A repeated side is a hazard when a consumer reads it:

    * an aggregate over columns fires when a column belongs to a repeated side;
    * ``COUNT(*)`` counts join rows, which equal an unrepeated side's row count, so it fires
      only when every side repeats (consistent with ``detect_cross_model_fanout``, #179);
    * an ungrouped, non-``DISTINCT`` projection fires when every side it reads repeats;
    * such a projection also fires when a key in ``declared_keys`` (the model's own, so the
      tree's last SELECT) reads only sides one join repeats for a cause outside them, since two
      output rows then share the key. A key that does not resolve through the projection is not
      judged.

    Duplicate-safe aggregates (and ``duplicate_safe_builtins`` UDFs) are not consumers (#170).
    Silent on a non-equality ON, ``CROSS``, SEMI and ANTI joins and the ``LEFT JOIN ... IS NULL``
    idiom.
    """
    scopes = _scope_index_for(tree, model_keys, model_fds, scope_index)
    outputs = {id(sel) for sel in _output_selects(tree)}
    root = scopes.get(id(tree))
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        chain = JoinChain(sel, lambda node: _source_facts(node, scopes))
        if not chain.joins:
            continue
        group = sg.group_of(sel)
        if group is not None and chain.grouped_to_one_row(group):
            continue
        consumers = _consumers(
            sel,
            frozenset(chain.sides),
            safe_builtins=duplicate_safe_builtins,
            is_output=id(sel) in outputs,
        )
        steps = range(len(chain.joins) + 1)
        repeated = [chain.repeated(p) for p in steps]
        key_sides = (
            _declared_key_sides(sel, chain, declared_keys, root)
            if sel is tree and consumers.rows is not None
            else ()
        )
        for p, join in enumerate(chain.joins, 1):
            newly = repeated[p] - repeated[p - 1]
            broken = [
                s for s in key_sides if chain.key_broken(s, p) and not chain.key_broken(s, p - 1)
            ]
            if broken:
                out.append(_fanout_finding(join, broken[0]))
            elif newly and consumers.hurt_by(
                newly, repeated=repeated[-1], sides=frozenset(chain.sides)
            ):
                out.append(_fanout_finding(join, newly))
    return tuple(out)


def _declared_keys(
    manifest: Manifest,
    uid: str,
    key_facts: Mapping[SourceRef, tuple[Fact[CandidateKeySet, SourceRef], ...]],
) -> frozenset[Key]:
    """A model's declared keys (tests, contracts) plus its ``unique_key`` config, which claims
    the grain whether or not the write path enforces it."""
    keys = declared_grain_keys(key_facts.get(SourceRef(SourceKind.MODEL, uid), ()))
    node = manifest.models.get(uid)
    unique_key = node.config.unique_key if node is not None and node.config is not None else ()
    return keys | {frozenset(col.lower() for col in unique_key)} if unique_key else keys


def _output_selects(node: Expr) -> Iterator[exp.Select]:
    """The SELECTs whose rows are the tree's output: the root, or the arms of a top-level set
    operation. A CTE or subquery SELECT feeds another scope instead."""
    if isinstance(node, exp.Select):
        yield node
    elif isinstance(node, exp.SetOperation | exp.Subquery):
        for arm in (node.this, node.args.get("expression")):
            if isinstance(arm, Expr):
                yield from _output_selects(arm)


def _declared_key_sides(
    sel: exp.Select, chain: JoinChain, declared_keys: frozenset[Key], root: Input | None
) -> list[frozenset[str]]:
    """The sides each declared key is read from, for keys the scope's own derived keys do not
    cover. A QUALIFY the derivation could not model may have deduplicated, so it judges nothing."""
    if root is None or (not root.exact and sg.qualify_of(sel) is not None):
        return []
    sides = [
        chain.projected_sides(key)
        for key in declared_keys
        if not covers(FDSet(root.fds), key, root.keys)
    ]
    return [s for s in sides if s]


def detect_limit_without_deterministic_order(
    tree: Expr,
    *,
    model_keys: ModelKeys,
    model_fds: Mapping[str, FDSet] = {},
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
    it to one row, so a ``LIMIT`` cannot drop a row. An order key spelled as a bare name is
    resolved through the projection's aliases before being matched, so an ``order by <alias>``
    of a key counts as covering (and a renamed non-key column does not pass as the key). A
    positional or qualified key already names a source column (``sg.OrderTarget``) and is
    matched as-is, so all three spellings of one key agree.
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
    source = _single_source(tree, _scope_index_for(tree, model_keys, model_fds, scope_index))
    if source is None:
        return ()
    targets = sg.statement_order_targets(tree)
    order_cols = _bare_column_names([t.expression for t in targets])
    if order_cols is None:
        return ()
    projection = _projection_aliases(tree)
    covered = frozenset(
        name if t.in_source_namespace else projection.get(name, name)
        for t, name in zip(targets, order_cols, strict=True)
    )
    if covers(FDSet(source.fds), covered, source.keys):
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


# The relation graph, the uniqueness annotations propagated over it, and the property that
# produced them. An audit computes this once and threads it into every factory, and the FD
# walk reads the same keys through its uniqueness edge rather than re-propagating them.
RelationUniqueness = tuple[
    RelationLineageGraph,
    Mapping[SourceRef, Annotation[CandidateKeySet]],
    Property[CandidateKeySet, SourceRef],
    Mapping[SourceRef, tuple[Fact[CandidateKeySet, SourceRef], ...]],
]


def relation_uniqueness(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
    graph: RelationLineageGraph | None = None,
    key_facts: tuple[Fact[CandidateKeySet, SourceRef], ...] = (),
) -> RelationUniqueness:
    """Build the relation graph and propagate the uniqueness property over it.

    ``propagate`` memoizes only within a single call, so the two detector factories that need
    this pair would otherwise each rebuild the graph and re-run the whole-manifest uniqueness
    fixpoint. :func:`dblect.audit.walker.run_audit` computes it once and passes it to both.
    ``parsed`` shares the audit's already-parsed trees; ``graph`` shares a relation graph the
    check family already built (``analyze`` threads it) so the build runs once per run, while the
    uniqueness fixpoint still runs here (the two families propagate different properties).
    ``key_facts`` adds keys the caller already resolved (Python contracts), the same channel
    the check family grounds the grain check through.
    """
    if graph is None:
        graph = build_relation_graph(manifest, dialect=profile.sqlglot_dialect, parsed=parsed).graph
    facts = uniqueness_facts(manifest, profile, extra_facts=key_facts, parsed=parsed)
    uniqueness = uniqueness_property_from_facts(facts)
    return graph, propagate(graph, uniqueness), uniqueness, facts


def fd_annotations_by_name(
    manifest: Manifest,
    graph: RelationLineageGraph,
    fd_facts: tuple[Fact[FDSet, SourceRef], ...] = (),
    *,
    relation_keys: RelationUniqueness | None = None,
) -> dict[str, FDSet]:
    """Propagate the functional-dependency property over the relation graph, indexed by the
    relation name as it appears in compiled SQL.

    Grounded from the declared ``determines`` facts the caller threads in; even with none, the
    property still derives structural FDs (a GROUP BY key, a join's ON equalities), sound on
    their own. Both the join-fanout detector (key coverage through ``determines``) and the
    join-on-nullable-key detector (folding a co-determined key column into its declared key)
    read this map, so :func:`dblect.audit.walker.run_audit` computes it once over the shared
    graph and threads it into both factories rather than re-running the fixpoint per factory.

    ``relation_keys`` (an already-propagated pass over this same ``graph``) wires the FD
    property's uniqueness edge, so a candidate key determines the columns selected alongside
    it."""
    ground = functional_dependency_grounding(by_scope(fd_facts))
    if relation_keys is None:
        fd_anns = propagate(graph, functional_dependency_property(ground))
    else:
        _, keys, uniqueness, _ = relation_keys
        store = AnnotationStore()
        for scope, ann in keys.items():
            store.record(uniqueness.name, scope, ann)
        fd_prop = functional_dependency_property(ground, uniqueness=uniqueness.ref)
        ctx = PropertyRegistry((uniqueness, fd_prop)).dep_context(store)
        fd_anns = propagate(graph, fd_prop, dep_context=ctx)
    return index_by_name(manifest, {ref: ann.value for ref, ann in fd_anns.items()})


def make_fact_grounded_detectors(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
    relation_keys: RelationUniqueness | None = None,
    fd_facts: tuple[Fact[FDSet, SourceRef], ...] = (),
    fd_by_name: Mapping[str, FDSet] | None = None,
) -> tuple[Detector, ...]:
    """Curry the fact-grounded detectors against the propagated uniqueness keys.

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
    relation_keys = (
        relation_keys
        if relation_keys is not None
        else relation_uniqueness(manifest, profile, parsed=parsed)
    )
    graph, keys, _uniqueness, key_facts = relation_keys
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
        fd_by_name = fd_annotations_by_name(manifest, graph, fd_facts, relation_keys=relation_keys)
    declared_by_tree = {
        id(tree): _declared_keys(manifest, uid, key_facts) for uid, tree in (parsed or {}).items()
    }
    cache: dict[int, ScopeIndex] = {}

    def scope_index(tree: Expr) -> ScopeIndex:
        hit = cache.get(id(tree))
        if hit is None:
            scope_flow = relation_scope_filters(tree, flow_by_name)
            hit = relation_scope_facts(
                tree,
                model_keys,
                model_fds=fd_by_name,
                conditional_by_name=conditional_by_name,
                scope_flow=scope_flow,
            )
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
            duplicate_safe_builtins=profile.duplicate_safe_aggregate_builtins,
            declared_keys=declared_by_tree.get(id(tree), frozenset()),
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
# Per relation, the columns that are value-functions of a single base column, mapped to it.
OriginsBySource = Mapping[SourceRef, Mapping[str, ColumnRef]]
NameToRef = Mapping[str, SourceRef]


def detect_cross_model_fanout(
    tree: Expr,
    *,
    name_to_ref: NameToRef,
    keys_by_source: KeysBySource,
    provenance_by_source: ProvenanceBySource,
    origins_by_source: OriginsBySource,
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
    This is the cross-model analog of the local join fan-out's collapse of grouped rows,
    and it clears the magnitude path's grouped-to-a-finer-grain case (``SUM(amount) GROUP BY
    order_id, item_id`` over line-grain staging) as well.

    Silent when the FROM is not a single ref'd relation (a join or a CTE/subquery needs
    column-level reasoning kept for later), when the aggregate is duplicate-safe, and when the
    origin relation has no known key (the firewall posture: with no grain to name, there is
    no positive fact to fire on). Also silent on a COUNT-behavior fold (``COUNT(*)``, ``COUNT(1)``,
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
        rel_origins = origins_by_source.get(ref, {})
        rel_keys = keys_by_source.get(ref, NO_KEYS)
        group_cols = _group_by_columns(sel)
        # Grain-collapse guard: when the relation is provably unique at the GROUP BY grain
        # (a candidate key fits within the grouping columns), every bucket is a single row, so
        # no fold over it can over-count, whatever magnitude it reads. This is the cross-model
        # analog of the local join fan-out's collapse of grouped rows.
        if group_cols is not None and grain_preserved(rel_keys, group_cols):
            continue
        for agg in _sensitive_aggregate_consumers(sel, safe_builtins=duplicate_safe_builtins):
            origin = _replicated_origin(
                agg,
                rel_keys=rel_keys,
                rel_prov=rel_prov,
                rel_origins=rel_origins,
                keys_by_source=keys_by_source,
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
    _, keys, _uniqueness, _ = (
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
    origins_by_source = _origins_by_source(col_graph)
    name_to_ref = index_by_name(manifest, {ref: ref for ref in keys_by_source})

    def fanout(tree: Expr) -> tuple[Finding, ...]:
        return detect_cross_model_fanout(
            tree,
            name_to_ref=name_to_ref,
            keys_by_source=keys_by_source,
            provenance_by_source=provenance_by_source,
            origins_by_source=origins_by_source,
            duplicate_safe_builtins=profile.duplicate_safe_aggregate_builtins,
        )

    return (fanout,)


def _single_from_ref(sel: exp.Select, name_to_ref: NameToRef) -> SourceRef | None:
    """The ``SourceRef`` of ``sel``'s FROM when it is a single ref'd relation with no joins.

    A join or a non-table FROM (subquery) needs column-level reasoning we keep for later, and
    a name shadowed by a CTE in ``sel``'s lexical scope (:func:`sg.cte_shadows`) is a
    per-query scope the propagator does not annotate, so all three return ``None`` and the
    detector stays silent.
    """
    if sg.joins_of(sel):
        return None
    from_ = sg.from_of(sel)
    if from_ is None or not isinstance(from_.this, exp.Table):
        return None
    table = from_.this
    if sg.cte_shadows(table):
        return None
    return name_to_ref.get(sg.table_relation_key(table))


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
    rel_origins: Mapping[str, ColumnRef],
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
        if not _origin_grain_preserved(rel_keys, rel_origins, origin, origin_keys):
            return origin
    return None


def _origin_grain_preserved(
    rel_keys: CandidateKeySet,
    rel_origins: Mapping[str, ColumnRef],
    origin: SourceRef,
    origin_keys: CandidateKeySet,
) -> bool:
    """True when the aggregated relation stays unique at some candidate grain of ``origin``.

    Each of the origin's candidate keys is translated into the relation's column names; the
    relation preserves the grain when a surviving key refines any translated origin key. A key
    whose columns the relation does not carry cannot witness the grain and is skipped.
    """
    for okey in origin_keys.keys:
        translated = _translate_key(okey, origin, rel_origins)
        if translated is not None and grain_preserved(rel_keys, translated):
            return True
    return False


def _translate_key(
    origin_key: Key, origin: SourceRef, rel_origins: Mapping[str, ColumnRef]
) -> Key | None:
    """``origin_key`` rewritten in the aggregated relation's column names, or ``None`` when the
    relation carries none of some origin key column.

    A column carries an origin column when its value is a function of that column alone
    (:func:`value_origin`), so a relation unique on the carrier is unique on the origin column.
    Every carrier joins the translation: a computed carrier such as ``UPPER(line_id)`` sits
    beside the bare ``line_id`` and must not displace it. A column that merely reads the origin
    column (``CONCAT``, a window) is not a carrier, since uniqueness on it says nothing of the
    origin's rows."""
    translated: set[str] = set()
    for origin_col in origin_key:
        target = ColumnRef(origin, origin_col)
        carriers = {name for name, o in rel_origins.items() if o == target}
        if not carriers:
            return None
        translated |= carriers
    return frozenset(translated)


def _origins_by_source(graph: ColumnLineageGraph) -> dict[SourceRef, dict[str, ColumnRef]]:
    """Group each column that is a value-function of one base column by its relation."""
    out: dict[SourceRef, dict[str, ColumnRef]] = {}
    for col in graph.subjects():
        origin = value_origin(graph, col)
        if origin is not None:
            out.setdefault(col.source, {})[col.column] = origin
    return out


def _provenance_by_source(
    provenance: Mapping[ColumnRef, Annotation[frozenset[ColumnRef]]],
) -> dict[SourceRef, dict[str, frozenset[ColumnRef]]]:
    """Group the per-column where-provenance by its relation, ``column -> source columns``."""
    by_source: dict[SourceRef, dict[str, frozenset[ColumnRef]]] = {}
    for col_ref, ann in provenance.items():
        by_source.setdefault(col_ref.source, {})[col_ref.column] = ann.value
    return by_source


def _scope_index_for(
    tree: Expr,
    model_keys: ModelKeys,
    model_fds: Mapping[str, FDSet],
    scope_index: ScopeIndex | None,
) -> ScopeIndex:
    """Resolve a per-scope index, computing one if the caller didn't supply it.

    Tests call the detectors directly without precomputing the index; the audit
    walker always supplies a cached one so this branch costs nothing in production.
    """
    if scope_index is not None:
        return scope_index
    return relation_scope_facts(tree, model_keys, model_fds=model_fds)


def _source_facts(node: Expr, scopes: ScopeIndex) -> Input | None:
    """The resolved facts of one FROM/JOIN source node; a subquery reads its inner
    SELECT's entry. ``None`` when the walk gave up on the node's shape."""
    key_node = node.this if isinstance(node, exp.Subquery) and isinstance(node.this, Expr) else node
    return scopes.get(id(key_node))


def _single_source(sel: exp.Select, scopes: ScopeIndex) -> Input | None:
    """Facts for ``sel``'s single FROM source, or ``None`` when the scope has a JOIN,
    no FROM, or a source with no known keys, so the order-key and LIMIT detectors
    stay silent."""
    from_ = sg.from_of(sel)
    if from_ is None or sg.joins_of(sel):
        return None
    facts = _source_facts(from_.this, scopes)
    if facts is None or not facts.keys:
        return None
    return facts


def _fanout_finding(join: exp.Join, repeated: frozenset[str]) -> Finding:
    on = sg.on_of(join)
    target = f"{sg.name_of(join.this)} on ({sg.render_sql(on)})" if on is not None else ""
    return Finding(
        kind=FindingKind.JOIN_FANOUT,
        message=(
            f"JOIN to {target} can repeat rows of {', '.join(sorted(repeated))}, and a "
            f"duplicate-sensitive consumer reads them. Either pin the join to a unique key or "
            f"aggregate the repeated side first."
        ),
        sql_snippet=sg.render_sql(join),
        line_start=_line_start(join),
        line_end=_line_end(join),
    )


@dataclass(frozen=True, slots=True)
class _Reads:
    """The output sides a consumer reads. ``unresolved`` marks a column whose side cannot be
    named (unqualified, or qualified by something outside the join), which may be any side."""

    sides: frozenset[str] = frozenset()
    unresolved: bool = False

    @staticmethod
    def of(columns: Iterable[exp.Column], sides: frozenset[str]) -> _Reads:
        named: set[str] = set()
        unresolved = False
        for c in columns:
            qualifier = (sg.column_table(c) or "").lower()
            if qualifier in sides:
                named.add(qualifier)
            else:
                unresolved = True
        return _Reads(frozenset(named), unresolved)


@dataclass(frozen=True, slots=True)
class _Consumers:
    """What reads a select's joined rows: aggregate column reads, a column-free count, and the
    reads of an ungrouped projection (``None`` under GROUP BY or DISTINCT)."""

    values: tuple[_Reads, ...]
    counts_rows: bool
    rows: _Reads | None

    def hurt_by(
        self, multiplied: frozenset[str], *, repeated: frozenset[str], sides: frozenset[str]
    ) -> bool:
        """True when a consumer is hurt by the ``multiplied`` sides repeating. ``repeated`` is
        every side repeating after the whole chain."""
        if any(r.sides & multiplied or r.unresolved for r in self.values):
            return True
        if self.counts_rows and sides <= repeated:
            return True
        rows = self.rows
        if rows is None or not rows.sides <= repeated:
            return False
        return bool(rows.sides & multiplied) or (not rows.sides and rows.unresolved)


def _consumers(
    sel: exp.Select, sides: frozenset[str], *, safe_builtins: frozenset[str], is_output: bool
) -> _Consumers:
    """Classify what reads ``sel``'s joined rows; see :class:`_Consumers`. A select that is not
    the model's output passes its rows to another scope, which may read any side."""
    values: list[_Reads] = []
    counts_rows = False
    for agg in _sensitive_aggregate_consumers(sel, safe_builtins=safe_builtins):
        columns = [c for c in sg.find_columns(agg) if _node_in_scope(c, sel)]
        behavior = aggregate_behavior(agg) if isinstance(agg, exp.AggFunc) else None
        if columns:
            values.append(_Reads.of(columns, sides))
        elif behavior is AggregateBehavior.COUNT:
            counts_rows = True
        else:
            values.append(_Reads(unresolved=True))  # sum(1), a UDF: no side to name
    rows: _Reads | None = None
    grouped = sg.group_of(sel) is not None or _is_implicit_single_group(sel)
    if not grouped and not sel.args.get("distinct"):
        rows = _row_reads(sel, sides) if is_output else _Reads(unresolved=True)
    return _Consumers(tuple(values), counts_rows, rows)


def _row_reads(sel: exp.Select, sides: frozenset[str]) -> _Reads:
    """The sides ``sel``'s projections read outside a collapsing aggregate. A bare ``*`` reads
    every side; ``c.*`` parses as a column of ``c``."""
    columns: list[exp.Column] = []
    star = False
    for root in sel.expressions:
        for node in root.walk():
            if not _node_in_scope(node, sel) or _under_collapsing_aggregate(node, sel):
                continue
            if isinstance(node, exp.Column):
                columns.append(node)
            elif isinstance(node, exp.Star) and not isinstance(node.parent, exp.Column):
                star = True
    reads = _Reads.of(columns, sides)
    return _Reads(sides | reads.sides if star else reads.sides, reads.unresolved)


def _is_implicit_single_group(sel: exp.Select) -> bool:
    """True when ``sel`` is an ungrouped aggregate whose every column read in a projection or
    HAVING term sits under a collapsing aggregate, so the whole select is one group with no
    per-row value. Columns of a nested sub-SELECT belong to that scope and are not weighed."""
    if not _is_single_row_scope(sel):
        return False
    roots: list[Expr] = list(sel.expressions)
    having = sel.args.get("having")
    if isinstance(having, exp.Having) and isinstance(having.this, Expr):
        roots.append(having.this)
    return not any(
        _node_in_scope(col, sel) and not _under_collapsing_aggregate(col, sel)
        for root in roots
        for col in root.find_all(exp.Column)
    )


def _under_collapsing_aggregate(node: Expr, sel: exp.Select) -> bool:
    """True when an aggregate of ``sel`` that is not a window function encloses ``node``."""
    cur = node.parent
    while cur is not None and cur is not sel:
        if isinstance(cur, exp.AggFunc) and not _within_window(cur, sel):
            return True
        cur = cur.parent
    return False


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
    order: list[Expr], grouping: list[Expr], source: Input
) -> tuple[list[str], list[str]] | None:
    """The bare order and grouping column names when their union does not cover any
    source key under the source's dependencies, so the order may not be total. ``None``
    when it is provably total or we cannot judge it: an empty order, or an order or
    grouping key that is not a bare column."""
    if not order:
        return None
    order_cols = _bare_column_names(order)
    grouping_cols = _bare_column_names(grouping)
    if order_cols is None or grouping_cols is None:
        return None
    key_set = frozenset(order_cols) | frozenset(grouping_cols)
    if covers(FDSet(source.fds), key_set, source.keys):
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
