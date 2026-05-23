"""Fact-grounded detectors that consume uniqueness facts about source models.

Two detectors live here, both opportunistic (they fire only when the project
gives us enough information to make a claim; they stay silent everywhere
else):

* `detect_non_unique_window_order_keys`: window functions whose combined
  (partition, order) columns aren't covered by any uniqueness fact on the
  scope's source (a ref'd model, a CTE, or an inline subquery whose facts
  propagation knows). Ties in the ordering produce non-deterministic
  rankings.
* `detect_join_fanout`: JOINs whose joined-in side has uniqueness facts but
  none of them covers the join's equality predicate columns. The joined-in
  side may be a ref'd model or an in-scope CTE; the propagation map tells
  us which keys hold for either.

Both consult the same per-tree propagation map (computed once per tree and
cached across the two detectors) so a CTE that pass-throughs a ref'd
model's keys is treated like the model for fact-coverage purposes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.manifest import Manifest
from dblect.sql import Finding, FindingKind
from dblect.sql import _sqlglot as sg
from dblect.sql._sqlglot import JoinSide
from dblect.uniqueness.facts import UniquenessFact
from dblect.uniqueness.propagation import ScopeFacts, propagate_facts

Detector = Callable[[Expr], tuple[Finding, ...]]


def detect_non_unique_window_order_keys(
    tree: Expr,
    *,
    facts: Mapping[str, tuple[UniquenessFact, ...]],
    model_name_to_uid: Mapping[str, str],
    propagation: Mapping[int, ScopeFacts] | None = None,
) -> tuple[Finding, ...]:
    """Flag window ORDER BYs whose partition+order keys aren't a unique tuple.

    A scope is checkable when its FROM resolves to a single relation with
    known facts (a ref'd model, an in-scope CTE, or an inline subquery whose
    output the propagation pass figured out) and there are no joins.
    Multi-source scopes need column-level lineage we don't yet model and
    stay silent.
    """
    prop = _propagation_for(tree, facts, model_name_to_uid, propagation)
    out: list[Finding] = []
    for sel in sg.find_all_selects(tree):
        source_keys = _single_source_keys(
            sel,
            facts=facts,
            model_name_to_uid=model_name_to_uid,
            propagation=prop,
        )
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
                # Expressions (`order by date_trunc(...)`) need a more careful
                # equivalence check; skip for now.
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
                        f"and no known uniqueness fact on the source covers "
                        f"the combined key set. Ties in the order keys produce a "
                        f"non-deterministic ranking; add a stable tiebreaker."
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
    facts: Mapping[str, tuple[UniquenessFact, ...]],
    model_name_to_uid: Mapping[str, str],
    propagation: Mapping[int, ScopeFacts] | None = None,
) -> tuple[Finding, ...]:
    """Flag JOINs whose joined-in side has facts that don't cover the join.

    For each JOIN whose joined-in side resolves to either an in-scope CTE
    (with propagated facts) or a ref'd model (with declared/propagated
    facts), we ask: does any known key fit within the right-side equality
    predicate columns? If yes, the join can't multiply rows. If no, we flag.

    The detector stays silent when:

    * The joined-in side isn't a relation we can resolve to known facts
      (e.g., a CTE the propagation pass couldn't ground, or a model with
      no known keys at all).
    * The ON predicate isn't a conjunction of equalities between bare
      columns where one side belongs to the joined-in alias and the other
      doesn't. Anything fancier (function calls, range comparisons, OR)
      gets skipped to keep the rule conservative.
    * The join is `CROSS`: an explicit cartesian product, not a
      fanout-by-accident.
    """
    prop = _propagation_for(tree, facts, model_name_to_uid, propagation)
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
                target.name,
                cte_bodies=cte_bodies,
                propagation=prop,
                facts=facts,
                model_name_to_uid=model_name_to_uid,
            )
            if not target_keys:
                continue
            on = sg.on_of(j)
            if on is None:
                continue
            joined_cols = sg.equality_cols_on_alias(on, target.alias_or_name)
            if joined_cols is None or not joined_cols:
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


def _resolve_target_keys(
    name: str,
    *,
    cte_bodies: Mapping[str, Expr],
    propagation: Mapping[int, ScopeFacts],
    facts: Mapping[str, tuple[UniquenessFact, ...]],
    model_name_to_uid: Mapping[str, str],
) -> frozenset[frozenset[str]]:
    """Keys for `name`, looked up as a CTE first, then as a model ref.

    Returning an empty set means "no known facts" — the join-fanout detector
    treats that as "stay silent." A local CTE always shadows a model with the
    same name; this matches SQL's resolution rules and avoids the over-claim
    the old `cte_names` carve-out was guarding against.
    """
    body = cte_bodies.get(name)
    if body is not None:
        sf = propagation.get(id(body))
        return sf.keys if sf is not None else frozenset()
    uid = model_name_to_uid.get(name)
    if uid is None:
        return frozenset()
    return frozenset(f.columns for f in facts.get(uid, ()))


def make_fact_grounded_detectors(
    manifest: Manifest, facts: Mapping[str, tuple[UniquenessFact, ...]]
) -> tuple[Detector, ...]:
    """Curry the fact-grounded detectors against an audit-scoped context.

    Returns a tuple of plain `Detector` callables
    (`Callable[[Expr], tuple[Finding, ...]]`) the walker drops into its
    detector pipeline. Each curried detector consults a shared per-tree
    propagation cache so the propagation pass runs at most once per tree
    no matter how many detectors consume it.
    """
    name_to_uid: dict[str, str] = {m.name: uid for uid, m in manifest.models.items()}
    cache: dict[int, Mapping[int, ScopeFacts]] = {}

    def get_propagation(tree: Expr) -> Mapping[int, ScopeFacts]:
        k = id(tree)
        hit = cache.get(k)
        if hit is None:
            hit = propagate_facts(tree, model_facts=facts, model_name_to_uid=name_to_uid)
            cache[k] = hit
        return hit

    def window_keys(tree: Expr) -> tuple[Finding, ...]:
        return detect_non_unique_window_order_keys(
            tree,
            facts=facts,
            model_name_to_uid=name_to_uid,
            propagation=get_propagation(tree),
        )

    def fanout(tree: Expr) -> tuple[Finding, ...]:
        return detect_join_fanout(
            tree,
            facts=facts,
            model_name_to_uid=name_to_uid,
            propagation=get_propagation(tree),
        )

    return (window_keys, fanout)


def _propagation_for(
    tree: Expr,
    facts: Mapping[str, tuple[UniquenessFact, ...]],
    model_name_to_uid: Mapping[str, str],
    propagation: Mapping[int, ScopeFacts] | None,
) -> Mapping[int, ScopeFacts]:
    """Resolve a propagation map for `tree`, computing one if the caller didn't.

    Tests usually call the detectors directly without going through
    ``make_fact_grounded_detectors``; computing the map on demand keeps those
    call sites short. The audit walker always supplies the precomputed map
    so this branch costs nothing in production.
    """
    if propagation is not None:
        return propagation
    return propagate_facts(tree, model_facts=facts, model_name_to_uid=model_name_to_uid)


def _single_source_keys(
    sel: exp.Select,
    *,
    facts: Mapping[str, tuple[UniquenessFact, ...]],
    model_name_to_uid: Mapping[str, str],
    propagation: Mapping[int, ScopeFacts],
) -> frozenset[frozenset[str]] | None:
    """Keys for ``sel``'s single FROM source, or ``None`` if not a clean single-source scope.

    A scope is single-source when ``FROM`` is a bare table reference and
    there are no JOINs. The source resolves to a CTE (whose body sits in
    the propagation map), an inline subquery (likewise), or a model ref.
    Returns the source's keys (possibly empty); ``None`` when the shape
    doesn't qualify and the window-keys detector should stay silent.
    """
    from_ = sg.from_of(sel)
    if from_ is None or from_.this is None:
        return None
    if sg.joins_of(sel):
        return None
    target = from_.this
    if not isinstance(target, exp.Table):
        return None
    cte_body = _cte_body_for(target.name, sel)
    if cte_body is not None:
        sf = propagation.get(id(cte_body))
        if sf is None or not sf.keys:
            return None
        return sf.keys
    uid = model_name_to_uid.get(target.name)
    if uid is None:
        return None
    source_facts = facts.get(uid)
    if not source_facts:
        return None
    return frozenset(f.columns for f in source_facts)


def _cte_body_for(name: str, sel: exp.Select) -> Expr | None:
    """The CTE body matching `name` in `sel`'s enclosing WITH, if any.

    Walks outward (`sel`'s own WITH, then parents') to honor lexical CTE
    scoping. A CTE defined in an outer WITH is visible inside an inner
    SELECT that doesn't redefine it.
    """
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
    """True when window `w` belongs to ``sel`` (not a nested sub-SELECT)."""
    node: Expr | None = w.parent
    while node is not None:
        if isinstance(node, exp.Select):
            return node is sel
        node = node.parent
    return False


def _bare_column_names(expressions: list[Expr]) -> list[str] | None:
    """Return column names if every expression is a bare ``exp.Column``; else None."""
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
