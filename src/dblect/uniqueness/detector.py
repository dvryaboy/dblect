"""Detector: window-function ordering keys that aren't grounded as unique.

A window function like ``row_number() over (partition by p order by k)`` is
deterministic only when ``(p, k)`` is unique within the input row scope.
Otherwise the ranking has ties and the result is non-deterministic: the same
input can produce different outputs across runs.

This detector is **opportunistic**: it fires only when the project gives us
enough information to make the claim. Specifically:

* The model's top-level FROM resolves to a single ref'd model (no joins at
  the top level). Multi-source joins need column-level lineage, which is a
  separate body of work.
* We have at least one uniqueness fact for the source model.
* The combined (partition, order) column set is *not* covered by any of
  those facts.

When any of those conditions fails, the detector stays silent. That's the
right posture: we're claiming a hazard, not policing every window function.
"""

from __future__ import annotations

from collections.abc import Mapping

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.manifest import Manifest
from dblect.sql import Finding, FindingKind, ParsedSQL
from dblect.sql import _sqlglot as sg
from dblect.uniqueness.facts import UniquenessFact


def detect_non_unique_window_order_keys(
    parsed: ParsedSQL,
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
    sel = _top_level_select(parsed.tree)
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


def make_detector(
    manifest: Manifest, facts: Mapping[str, tuple[UniquenessFact, ...]]
):
    """Curry the detector against an audit-scoped context.

    Returns a plain ``Detector`` (``Callable[[ParsedSQL], tuple[Finding, ...]]``)
    so the walker can drop it into the existing detector pipeline.
    """
    name_to_uid: dict[str, str] = {m.name: uid for uid, m in manifest.models.items()}

    def detector(parsed: ParsedSQL) -> tuple[Finding, ...]:
        return detect_non_unique_window_order_keys(
            parsed,
            facts=facts,
            model_name_to_uid=name_to_uid,
        )

    return detector


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
