"""Value-effect guards: one catalog answering whether a padding NULL still reaches a
set-level consumer.

An outer join's optional side pads its columns with NULL on unmatched rows (the value
effect). That NULL is harmless until it reaches a consumer whose result it changes: a
``GROUP BY`` key forms a phantom bucket, a null-intolerant ``WHERE`` comparison drops the
unmatched row and silently inverts the join. It is harmless again when a guard
neutralises it on the path between the padding and the consumer. The detectors in the
outer-join nullability family ask this catalog whether their consumer is guarded instead
of each re-deriving "can a padding NULL still be seen here" privately, so a ``COALESCE``
nested inside an ``OR`` is cleared by the union of the rules with no per-detector code.

The guards read one fact about the surrounding query: which relation aliases the outer
join NULL-pads (``nullable``, from :func:`outer_join_optional_aliases`). A column whose
qualifier is not among them is drawn from a relation the join keeps present, so it is a
guaranteed-present value for the fallback rules. An unqualified column carries no alias to
check, so it is treated as not-guaranteed-present: the firewall posture keeps firing under
uncertainty rather than clearing on a guess.

The catalog is the evidence side of the broad-net posture (``docs/design/hazard-algebra.md``):
each guard recognises a proven-safe shape, and an unrecognised shape keeps firing, so an
incomplete catalog costs a false positive, never a false negative.
"""

from __future__ import annotations

from collections.abc import Iterator

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import _sqlglot as sg


def is_coalesced(col: exp.Column, *, until: Expr) -> bool:
    """True if ``col`` passes through a ``COALESCE``/``IFNULL``/``NVL`` between itself and
    ``until`` (inclusive).

    The fallback replaces the padding NULL with some other value before the consumer reads
    it, so the analyst has taken the nullable case into their own hands. This is the
    permissive form: it clears regardless of what the fallback is, which is the right call
    where any deliberate handling defuses the hazard (a ``WHERE`` comparison). Where the
    fallback's own nullability matters (a ``GROUP BY`` key), use :func:`supplies_present_value`.
    """
    return any(isinstance(node, exp.Coalesce) for node in _path(col, until))


def is_null_checked(col: exp.Column, *, until: Expr) -> bool:
    """True if ``col`` sits inside an ``IS [NOT] NULL`` check between itself and ``until``.

    The check yields a boolean, never NULL, so the analyst is handling the nullable case
    explicitly: ``b.k IS NOT NULL`` as a predicate, or ``GROUP BY b.k IS NOT NULL`` whose
    buckets are the two booleans rather than a phantom NULL group.
    """
    return any(isinstance(node, exp.Is) for node in _path(col, until))


def supplies_present_value(col: exp.Column, *, until: Expr, nullable: frozenset[str]) -> bool:
    """True if ``col`` passes through a ``COALESCE`` whose argument list carries a value the
    join cannot make NULL: a literal (a column-free expression), or an expression whose
    columns are all drawn from relations the join keeps present (not in ``nullable``).

    Such a ``COALESCE`` always yields a present value, so a downstream consumer never sees
    the padding NULL. This is the strict form of :func:`is_coalesced`: ``coalesce(meta.key,
    base.key)`` over ``base LEFT JOIN meta`` clears (``base`` is the preserved side), but
    ``coalesce(b.key, c.key)`` over two outer joins still fires (both sides are nullable, so
    the merged key can genuinely be NULL).
    """
    return any(
        isinstance(node, exp.Coalesce) and _has_present_arg(node, nullable)
        for node in _path(col, until)
    )


def rescued_by_or_sibling(predicate: Expr, *, where: exp.Where, nullable: frozenset[str]) -> bool:
    """True if ``predicate`` is one term of a top-level ``OR`` at the ``WHERE`` root and a
    sibling disjunct keeps the unmatched rows alive, so the disjunction is join-preserving.

    A nullable-side comparison drops an unmatched row only when it sits in a conjunctive
    position. Under a top-level ``OR``, a row survives whenever any single term holds, so a
    sibling disjunct that references at least one column, none of them from the nullable
    sides ``predicate`` constrains, can carry the rows where ``predicate``'s side did not
    match. ``where a.x > 0 or b.y > 0`` over ``a LEFT JOIN b`` does not invert the join: an
    unmatched-``b`` row survives via ``a.x > 0``. A same-side ``OR`` (``b.y > 0 or b.z >
    0``) is not rescued, and an ``AND`` at the root has no top-level disjunction to rescue
    anything, so both keep firing.
    """
    root = _unparen(where.this)
    disjuncts = _top_level_disjuncts(root)
    if len(disjuncts) < 2:
        return False
    own = _enclosing_disjunct(predicate, disjuncts)
    if own is None:
        return False
    constrained = {
        table for c in sg.find_columns(predicate) if (table := sg.column_table(c)) in nullable
    }
    for sibling in disjuncts:
        if sibling is own:
            continue
        sibling_cols = sg.find_columns(sibling)
        if sibling_cols and not any(sg.column_table(c) in constrained for c in sibling_cols):
            return True
    return False


def _path(col: exp.Column, until: Expr) -> Iterator[Expr]:
    """The chain of nodes from ``col`` up to and including ``until``.

    Inclusive of ``until`` so a guard that *is* the boundary node (``GROUP BY
    coalesce(...)``, where the coalesce is the group expression itself) is still seen.
    """
    node: Expr | None = col
    while node is not None:
        yield node
        if node is until:
            return
        node = node.parent


def _has_present_arg(coalesce: exp.Coalesce, nullable: frozenset[str]) -> bool:
    for arg in (coalesce.this, *coalesce.expressions):
        if not isinstance(arg, Expr):
            continue
        cols = sg.find_columns(arg)
        if not cols:
            return True  # a literal or other column-free expression is always present
        if all(_is_present(c, nullable) for c in cols):
            return True
    return False


def _is_present(col: exp.Column, nullable: frozenset[str]) -> bool:
    """True if ``col`` is drawn from a relation the join keeps present. An unqualified
    column has no alias to vouch for, so it is not treated as present (keep firing)."""
    table = sg.column_table(col)
    return table is not None and table not in nullable


def _unparen(expr: Expr) -> Expr:
    while isinstance(expr, exp.Paren) and isinstance(expr.this, Expr):
        expr = expr.this
    return expr


def _top_level_disjuncts(expr: Expr) -> list[Expr]:
    """Flatten the ``OR`` tree at ``expr``. A non-``OR`` yields a single disjunct, so a
    conjunctive root has nothing to rescue."""
    inner = _unparen(expr)
    if isinstance(inner, exp.Or):
        return _top_level_disjuncts(inner.this) + _top_level_disjuncts(inner.expression)
    return [inner]


def _enclosing_disjunct(predicate: Expr, disjuncts: list[Expr]) -> Expr | None:
    """The disjunct whose subtree contains ``predicate``, found by walking ``predicate``'s
    ancestors until one is a disjunct."""
    ids = {id(d) for d in disjuncts}
    node: Expr | None = predicate
    while node is not None:
        if id(node) in ids:
            return node
        node = node.parent
    return None
