"""Fact-grounded audit detectors that consume relation-scoped uniqueness keys.

Two opportunistic detectors (they fire only when the project gives enough
information to make a claim, and stay silent otherwise):

* ``detect_non_unique_window_order_keys``: window functions whose combined
  (partition, order) columns are not covered by any candidate key of the scope's
  single source. Ties in the ordering produce non-deterministic rankings.
* ``detect_join_fanout``: JOINs whose joined-in side has known keys, none of
  which is covered by the join's equality predicate columns, so the join can
  multiply rows.

Both read keys from the lineage.facts uniqueness substrate: per-model keys come
from cross-model propagation (``uniqueness_property`` over the relation graph),
and a per-tree scope index (``relation_scope_keys``) supplies the keys of CTE and
inline-subquery scopes, which are not relations the propagator annotates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeVar

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.lineage.builder import build_relation_graph
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.predicate_flow import (
    predicate_flow_property,
    relation_scope_filters,
)
from dblect.lineage.properties.uniqueness import (
    Key,
    activate_conditional,
    activated_scope_keys,
    relation_scope_keys,
    uniqueness_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import Manifest
from dblect.sql import Finding, FindingKind
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
) -> tuple[Finding, ...]:
    """Flag JOINs whose joined-in side has keys that don't cover the join.

    For each JOIN whose joined-in side resolves to keys (an in-scope CTE or a
    ref'd model), we ask whether any known key fits within the right-side equality
    predicate columns. If yes, the join cannot multiply rows. If no, we flag.

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


def make_fact_grounded_detectors(
    manifest: Manifest,
    *,
    dialect: str | None = "duckdb",
    parsed: Mapping[str, Expr] | None = None,
) -> tuple[Detector, ...]:
    """Curry the fact-grounded detectors against substrate-derived keys.

    Per-model keys come from one cross-model propagation of the uniqueness
    property over the relation graph; ``parsed`` lets the caller share the audit's
    already-parsed trees so the graph build does not re-parse. Each curried
    detector consults a per-tree scope index, cached so the relation walk runs at
    most once per tree no matter how many detectors consume it.
    """
    graph = build_relation_graph(manifest, dialect=dialect, parsed=parsed).graph
    keys = propagate(graph, uniqueness_property(manifest))
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
    model_keys = _by_name(manifest, activated, lambda cks: cks.keys)
    conditional_by_name = _by_name(manifest, keys, lambda ann: ann.value.conditional)
    flow_by_name = _by_name(manifest, flow, lambda ann: ann.value)
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
        return detect_join_fanout(tree, model_keys=model_keys, scope_index=scope_index(tree))

    return (window_keys, fanout)


_V = TypeVar("_V")
_R = TypeVar("_R")


def _by_name(
    manifest: Manifest, anns: Mapping[SourceRef, _V], extract: Callable[[_V], _R]
) -> dict[str, _R]:
    """Index a per-relation value by the relation name as it appears in compiled SQL.

    A source resolves under ``identifier or name`` (dbt compiles
    ``{{ source(...) }}`` to its ``identifier``, which can diverge from ``name``);
    a model resolves under ``name``. This must match the relation-graph builder's
    ``_build_name_to_source`` so a name the detectors look up by lands on the same
    relation the propagation annotated. Models win over sources on a name collision
    (applied last), matching how a ``ref`` resolves. ``extract`` pulls the field the
    caller wants (keys, conditional keys, or flow) from each annotation.
    """
    by_name: dict[str, _R] = {}
    models: dict[str, _R] = {}
    for ref, value in anns.items():
        node = manifest.nodes.get(ref.unique_id)
        if node is None:
            continue
        target = models if ref.kind is SourceKind.MODEL else by_name
        target[node.identifier or node.name] = extract(value)
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
