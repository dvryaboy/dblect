"""Fact-grounded audit detectors that consume propagated lineage properties.

Opportunistic detectors (they fire only when the project gives enough information
to make a claim, and stay silent otherwise):

* ``detect_non_unique_window_order_keys``: window functions whose combined
  (partition, order) columns are not covered by any candidate key of the scope's
  single source. Ties in the ordering produce non-deterministic rankings.
* ``detect_join_fanout``: JOINs whose joined-in side has known keys, none of
  which is covered by the join's equality predicate columns, so the join can
  multiply rows.
* ``detect_cross_model_fanout``: a duplicate-sensitive aggregate that folds a
  magnitude an upstream fan-out replicated, over a relation no longer keyed at the
  magnitude's grain. This one also reads ``where_provenance`` to find the origin a
  magnitude traces to, the grain ``grain_preserved`` is asked about.

The first two read keys from the lineage.facts uniqueness substrate: per-model keys
come from cross-model propagation (``uniqueness_property`` over the relation graph),
and a per-tree scope index (``relation_scope_keys``) supplies the keys of CTE and
inline-subquery scopes, which are not relations the propagator annotates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import TypeVar

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import AdapterProfile
from dblect.lineage.builder import build_manifest_graph, build_relation_graph
from dblect.lineage.facts.model import Annotation
from dblect.lineage.graph import ColumnRef, RelationLineageGraph, SourceKind, SourceRef
from dblect.lineage.properties import where_provenance
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
from dblect.manifest import Manifest
from dblect.sql import Finding, FindingKind, duplicate_sensitive
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
            if not _window_is_in_scope(w, sel):
                continue
            order = sg.order_of(w)
            if order is None or not order.expressions:
                continue
            order_cols = _bare_column_names(order.expressions)
            partition_cols = _bare_column_names(sg.partition_of(w))
            if order_cols is None or partition_cols is None:
                # We only reason about windows whose keys are bare columns.
                # Expressions (`order by date_trunc(...)`) need an equivalence
                # check we don't model yet; skip.
                continue
            key_set = frozenset(order_cols) | frozenset(partition_cols)
            if any(k <= key_set for k in source_keys):
                continue
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


def detect_join_fanout(
    tree: Expr,
    *,
    model_keys: ModelKeys,
    scope_index: ScopeIndex | None = None,
    duplicate_safe_builtins: frozenset[str] = frozenset(),
) -> tuple[Finding, ...]:
    """Flag JOINs whose joined-in side has keys that don't cover the join.

    For each JOIN whose joined-in side resolves to keys (an in-scope CTE or a
    ref'd model), we ask whether any known key fits within the right-side equality
    predicate columns. If yes, the join cannot multiply rows. If no, we flag.

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
    joined-in alias, or on a ``CROSS`` join (an explicit cartesian product).
    """
    scopes = _scope_index_for(tree, model_keys, scope_index)
    cte_bodies: Mapping[str, Expr] = {
        cte.alias_or_name: cte.this for cte in tree.find_all(exp.CTE) if isinstance(cte.this, Expr)
    }
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        for j in sg.joins_of(sel):
            if sg.join_side_of(j) is JoinSide.CROSS:
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
            if any(k <= joined_cols for k in target_keys):
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


# The relation graph and the uniqueness annotations propagated over it. The fact-grounded
# and cross-model fan-out factories both rest on this pair, so an audit computes it once and
# threads it into both rather than re-running the fixpoint per factory.
RelationUniqueness = tuple[RelationLineageGraph, Mapping[SourceRef, Annotation[CandidateKeySet]]]


def relation_uniqueness(
    manifest: Manifest, profile: AdapterProfile, *, parsed: Mapping[str, Expr] | None = None
) -> RelationUniqueness:
    """Build the relation graph and propagate the uniqueness property over it.

    ``propagate`` memoizes only within a single call, so the two detector factories that need
    this pair would otherwise each rebuild the graph and re-run the whole-manifest uniqueness
    fixpoint. :func:`dblect.audit.walker.run_audit` computes it once and passes it to both;
    a standalone caller that omits it gets a fresh propagation. ``parsed`` shares the audit's
    already-parsed trees so the graph build does not re-parse.
    """
    graph = build_relation_graph(manifest, dialect=profile.sqlglot_dialect, parsed=parsed).graph
    keys = propagate(graph, uniqueness_property(manifest, profile, parsed=parsed))
    return graph, keys


def make_fact_grounded_detectors(
    manifest: Manifest,
    profile: AdapterProfile,
    *,
    parsed: Mapping[str, Expr] | None = None,
    relation_keys: RelationUniqueness | None = None,
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
    """
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
    model_keys = _by_name(manifest, activated, lambda _ref, cks: cks.keys)
    conditional_by_name = _by_name(manifest, keys, lambda _ref, ann: ann.value.conditional)
    flow_by_name = _by_name(manifest, flow, lambda _ref, ann: ann.value)
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

    def fanout(tree: Expr) -> tuple[Finding, ...]:
        return detect_join_fanout(
            tree,
            model_keys=model_keys,
            scope_index=scope_index(tree),
            duplicate_safe_builtins=profile.duplicate_safe_aggregate_builtins,
        )

    return (window_keys, fanout)


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

    Silent when the FROM is not a single ref'd relation (a join or a CTE/subquery needs
    column-level reasoning kept for later), when the aggregate is duplicate-safe, and when the
    origin relation has no known key, the firewall posture: with no grain to name there is no
    positive fact to fire on. Also silent on a star-argument fold (``COUNT(*)``): it names no
    column, so there is no magnitude to trace to an origin grain. A star count over a
    fanned-out relation does double count, so closing that gap (it needs the relation's own
    row grain rather than a traced origin) is tracked as future work, not handled here.
    """
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        ref = _single_from_ref(sel, name_to_ref)
        if ref is None:
            continue
        rel_prov = provenance_by_source.get(ref, {})
        rel_keys = keys_by_source.get(ref, NO_KEYS)
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
) -> tuple[Detector, ...]:
    """Curry the cross-model fan-out detector against two propagated properties.

    Uniqueness comes from the relation graph (which relation is keyed at which grain) and
    where-provenance from the column graph (which source a magnitude traces to). Both are
    propagated once over the whole manifest; ``parsed`` shares the audit's already-parsed
    trees so neither graph re-parses. ``relation_keys`` lets the audit pass the
    already-propagated uniqueness (see :func:`relation_uniqueness`) so the fixpoint, also
    needed by :func:`make_fact_grounded_detectors`, is not run twice.
    """
    _, keys = (
        relation_keys
        if relation_keys is not None
        else relation_uniqueness(manifest, profile, parsed=parsed)
    )
    keys_by_source: dict[SourceRef, CandidateKeySet] = {ref: ann.value for ref, ann in keys.items()}

    col_graph = build_manifest_graph(manifest, dialect=profile.sqlglot_dialect, parsed=parsed).graph
    provenance = propagate(col_graph, where_provenance)
    provenance_by_source = _provenance_by_source(provenance)
    name_to_ref = _by_name(manifest, keys_by_source, lambda ref, _v: ref)

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
    """
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


_V = TypeVar("_V")
_R = TypeVar("_R")


def _by_name(
    manifest: Manifest, anns: Mapping[SourceRef, _V], extract: Callable[[SourceRef, _V], _R]
) -> dict[str, _R]:
    """Index a per-relation value by the relation name as it appears in compiled SQL.

    A source resolves under ``identifier or name`` (dbt compiles
    ``{{ source(...) }}`` to its ``identifier``, which can diverge from ``name``);
    a model resolves under ``name``. This must match the relation-graph builder's
    ``_build_name_to_source`` so a name the detectors look up by lands on the same
    relation the propagation annotated. Models win over sources on a name collision
    (applied last), matching how a ``ref`` resolves. ``extract`` pulls the value the
    caller wants (keys, conditional keys, flow, or the ``SourceRef`` itself) from each
    relation's ``(ref, annotation)`` pair.
    """
    by_name: dict[str, _R] = {}
    models: dict[str, _R] = {}
    for ref, value in anns.items():
        node = manifest.nodes.get(ref.unique_id)
        if node is None:
            continue
        target = models if ref.kind is SourceKind.MODEL else by_name
        target[node.identifier or node.name] = extract(ref, value)
    by_name.update(models)
    return by_name


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


def _window_is_in_scope(w: exp.Window, sel: exp.Select) -> bool:
    """True when window ``w`` belongs to ``sel`` (not a nested sub-SELECT)."""
    node: Expr | None = w.parent
    while node is not None:
        if isinstance(node, exp.Select):
            return node is sel
        node = node.parent
    return False


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
