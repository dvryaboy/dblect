"""Fact-grounded detectors that consume uniqueness facts about source models.

Two detectors live here, both opportunistic (they fire only when the project
gives us enough information to make a claim; they stay silent everywhere
else):

* `detect_non_unique_window_order_keys`: window functions whose combined
  (partition, order) columns aren't covered by any declared uniqueness fact
  on the source model. Ties in the ordering produce non-deterministic
  rankings.
* `detect_join_fanout`: JOINs to a ref'd model where the model has declared
  uniqueness facts but none of them covers the join's equality predicate
  columns. The join can multiply rows.

Both share the same context (the manifest's model-name-to-uid index plus the
precomputed uniqueness facts) and are curried by `make_fact_grounded_detectors`
into plain `Detector` callables the walker drops into its detector pipeline.
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

Detector = Callable[[Expr], tuple[Finding, ...]]


def detect_non_unique_window_order_keys(
    tree: Expr,
    *,
    facts: Mapping[str, tuple[UniquenessFact, ...]],
    model_name_to_uid: Mapping[str, str],
) -> tuple[Finding, ...]:
    """Flag window ORDER BYs whose partition+order keys aren't a unique tuple.

    Only the top-level SELECT is inspected, and only when it reads from a
    single ref'd model. The check is silent for everything else: multi-join
    queries, queries reading from CTEs that we can't trace, queries where the
    source has no declared uniqueness facts.
    """
    sel = _top_level_select(tree)
    if sel is None:
        return ()
    source_uid = _single_source_model_uid(sel, model_name_to_uid)
    if source_uid is None:
        return ()
    source_facts = facts.get(source_uid)
    if not source_facts:
        return ()
    out: list[Finding] = []
    for w in sg.find_all_windows(sel):
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
        if _key_covered_by_facts(key_set, source_facts):
            continue
        rendered = _rendered(w)
        out.append(
            Finding(
                kind=FindingKind.NON_UNIQUE_WINDOW_ORDER_KEYS,
                message=(
                    f"window {rendered} orders by {sorted(order_cols)} "
                    f"partitioned by {sorted(partition_cols) or '()'}, "
                    f"and no declared uniqueness fact on the source covers "
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
) -> tuple[Finding, ...]:
    """Flag JOINs to a ref'd model whose declared keys don't cover the join.

    For each JOIN whose joined-in side resolves to a known model, we look up
    the model's uniqueness facts. If the facts exist and at least one is
    covered by the join's right-side equality predicate columns, the join
    can't multiply rows (the joined-in side is unique on those keys, so each
    left row matches at most one right row). Otherwise, we flag.

    The detector stays silent when:

    * The joined-in side isn't a known model (subqueries, CTEs, anything
      we can't resolve to a manifest node).
    * The model has no uniqueness facts at all (opportunistic posture: we
      don't claim a hazard without grounding).
    * The ON predicate isn't a conjunction of equalities between bare
      columns where one side belongs to the joined-in alias and the other
      doesn't. Anything fancier (function calls, range comparisons, OR)
      gets skipped to keep the rule conservative.
    * The join is `CROSS`: that's an explicit cartesian product, not a
      fanout-by-accident.
    """
    out: list[Finding] = []
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    for sel in sg.find_all_selects(tree):
        for j in sg.joins_of(sel):
            if sg.join_side_of(j) is JoinSide.CROSS:
                continue
            target = j.this
            if not isinstance(target, exp.Table):
                continue
            if target.name in cte_names:
                # A local CTE shadows the model name; we can't assume the
                # model's declared keys apply to this join.
                continue
            target_uid = model_name_to_uid.get(target.name)
            if target_uid is None:
                continue
            target_facts = facts.get(target_uid)
            if not target_facts:
                continue
            on = sg.on_of(j)
            if on is None:
                continue
            joined_cols = _equality_cols_on_alias(on, target.alias_or_name)
            if joined_cols is None or not joined_cols:
                continue
            if any(fact.columns <= joined_cols for fact in target_facts):
                continue
            sample_keys = ", ".join(sorted(joined_cols))
            known_keys = "; ".join(
                "(" + ", ".join(sorted(f.columns)) + ")" for f in target_facts
            )
            out.append(
                Finding(
                    kind=FindingKind.JOIN_FANOUT,
                    message=(
                        f"JOIN to {target.name} on ({sample_keys}) isn't covered by any "
                        f"declared uniqueness key on {target.name} (known: {known_keys}); "
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
    manifest: Manifest, facts: Mapping[str, tuple[UniquenessFact, ...]]
) -> tuple[Detector, ...]:
    """Curry the fact-grounded detectors against an audit-scoped context.

    Returns a tuple of plain `Detector` callables
    (`Callable[[Expr], tuple[Finding, ...]]`) the walker drops into its
    detector pipeline.
    """
    name_to_uid: dict[str, str] = {m.name: uid for uid, m in manifest.models.items()}

    def window_keys(tree: Expr) -> tuple[Finding, ...]:
        return detect_non_unique_window_order_keys(
            tree, facts=facts, model_name_to_uid=name_to_uid
        )

    def fanout(tree: Expr) -> tuple[Finding, ...]:
        return detect_join_fanout(tree, facts=facts, model_name_to_uid=name_to_uid)

    return (window_keys, fanout)


def _top_level_select(tree: Expr) -> exp.Select | None:
    if isinstance(tree, exp.Select):
        return tree
    if isinstance(tree, exp.With):
        body = tree.this
        if isinstance(body, exp.Select):
            return body
    return None


def _single_source_model_uid(
    sel: exp.Select, model_name_to_uid: Mapping[str, str]
) -> str | None:
    """The ref'd model `sel` reads from, or `None` if not a clean single source.

    Returns ``None`` if there are joins at the top level, if the FROM target
    isn't a bare table reference, or if the referenced name doesn't resolve
    to a known model.
    """
    from_ = sg.from_of(sel)
    if from_ is None or from_.this is None:
        return None
    if sg.joins_of(sel):
        return None
    target = from_.this
    if not isinstance(target, exp.Table):
        return None
    if target.args.get("alias") is not None:
        # An alias is fine; we still need to read the underlying name.
        pass
    name = target.name
    return model_name_to_uid.get(name)


def _equality_cols_on_alias(predicate: Expr, alias: str) -> frozenset[str] | None:
    """Columns on `alias` appearing in conjunctive equalities in `predicate`.

    Walks the AND-conjunction of `predicate`; for each leaf, accepts only
    `exp.EQ` between two bare columns where exactly one column's qualifier
    equals `alias`. Returns the set of column names on the `alias` side.
    Returns ``None`` if `predicate` contains anything other than a
    conjunction of such equalities (a disjunction, a function call,
    a range comparison, or an equality whose alias mix is ambiguous).
    """
    cols: set[str] = set()
    for leaf in _conjunctive_leaves(predicate):
        if not isinstance(leaf, exp.EQ):
            return None
        left = leaf.this
        right = leaf.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            return None
        left_alias = sg.column_table(left)
        right_alias = sg.column_table(right)
        on_alias = [c for c, t in ((left, left_alias), (right, right_alias)) if t == alias]
        off_alias = [c for c, t in ((left, left_alias), (right, right_alias)) if t != alias]
        if len(on_alias) != 1 or len(off_alias) != 1:
            return None
        cols.add(sg.column_name(on_alias[0]))
    return frozenset(cols)


def _conjunctive_leaves(predicate: Expr) -> list[Expr]:
    """Flatten an `AND`-only conjunction into its leaves; non-AND nodes are leaves."""
    if isinstance(predicate, exp.And):
        return [*_conjunctive_leaves(predicate.this), *_conjunctive_leaves(predicate.expression)]
    return [predicate]


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


def _key_covered_by_facts(
    key_set: frozenset[str], facts: tuple[UniquenessFact, ...]
) -> bool:
    """True if any uniqueness fact's column set is a subset of `key_set`.

    If `(a)` is declared unique on the source and the window key is
    `(a, ts)`, the window key is unique by extension: a superkey is still a
    key. So a fact covers a key set when its columns are a subset.
    """
    return any(fact.columns <= key_set for fact in facts)


def _rendered(w: exp.Window) -> str:
    return sg.render_sql(w)


def _line_start(node: Expr) -> int:
    span = sg.line_range(node)
    return span[0] if span is not None else 0


def _line_end(node: Expr) -> int:
    span = sg.line_range(node)
    return span[1] if span is not None else 0
