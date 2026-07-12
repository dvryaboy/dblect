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
from dblect.sql._sqlglot import JoinSide


def is_coalesced(col: exp.Column, *, until: Expr) -> bool:
    """True if ``col`` passes through a ``COALESCE``/``IFNULL``/``NVL`` between itself and
    ``until`` (inclusive).

    The fallback replaces the padding NULL with some other value before the consumer reads
    it, so the analyst has taken the nullable case into their own hands. This is the
    permissive form: it clears regardless of what the fallback is, which is the right call
    where any deliberate handling defuses the hazard (a ``WHERE`` comparison). Where the
    fallback's own nullability matters (a ``GROUP BY`` key), use :func:`supplies_present_value`.
    """
    return any(isinstance(n, exp.Coalesce) for n in _path(col, until))


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
    sibling disjunct rescues the rows ``predicate`` would drop only when it references a
    relation guaranteed present on exactly those rows (:func:`_present_when_padded`). A
    preserved side qualifies (``where a.x > 0 or b.y > 0`` over ``a LEFT JOIN b``: an
    unmatched-``b`` row survives via ``a.x > 0``), and so does the partner of a single FULL
    OUTER join (``where l.v > 0 or r.v > 0``, since a full join populates one side per
    unmatched row). A sibling resting on another independent optional side does not
    (``where b.y > 0 or c.z > 0`` over ``a LEFT JOIN b LEFT JOIN c`` keeps firing: a row
    matching neither ``b`` nor ``c`` is dropped). A same-side ``OR`` and an ``AND`` at the
    root keep firing for the same reason.
    """
    sel = where.parent
    if not isinstance(sel, exp.Select):
        return False
    disjuncts = _top_level_disjuncts(_unparen(where.this))
    if len(disjuncts) < 2:
        return False
    own = _enclosing_disjunct(predicate, disjuncts)
    if own is None:
        return False
    constrained = frozenset(
        table for c in sg.find_columns(predicate) if (table := sg.column_table(c)) in nullable
    )
    present = _present_when_padded(sel, constrained=constrained, nullable=nullable)
    for sibling in disjuncts:
        if sibling is own:
            continue
        if any(sg.column_table(c) in present for c in sg.find_columns(sibling)):
            return True
    return False


def defaulted_true_by_coalesce(predicate: Expr, *, until: Expr) -> bool:
    """True if ``predicate`` is the first argument of a ``COALESCE`` defaulting to the literal
    ``TRUE``, and that ``COALESCE`` reaches ``until`` through boolean connectives only, so an
    unmatched row (where the predicate is NULL) is defaulted to a kept ``TRUE`` and the outer
    join is not inverted.

    Sound rather than permissive about the default *and* its context. ``coalesce(pred, false)``
    and a non-literal or fallback-position default keep firing, since the padding ``TRUE`` is not
    proven there. A proven ``TRUE`` that a ``NOT`` or a re-comparison of the value
    (``coalesce(pred, true) = false``) flips back into a drop keeps firing too: the ``COALESCE``
    no longer proves the row survives once its value passes through a non-truthy context.
    """
    prev: Expr | None = None
    for node in _path(predicate, until):
        if (
            isinstance(node, exp.Coalesce)
            and prev is node.this
            and node.expressions
            and all(_is_true_literal(d) for d in node.expressions)
        ):
            return _reaches_root_truthy(node, until)
        prev = node
    return False


def _reaches_root_truthy(node: Expr, until: Expr) -> bool:
    """True if a subexpression evaluating to ``TRUE`` keeps its row: every node strictly between
    ``node`` and the WHERE root ``until`` is a boolean AND/OR connective (optionally
    parenthesised). Under a ``NOT`` or a comparison of the value a ``TRUE`` can still drop the
    row, so a defaulted ``TRUE`` there does not prove the join is preserved."""
    for anc in _path(node, until):
        if anc is node or anc is until:
            continue
        if not isinstance(anc, (exp.And, exp.Or, exp.Paren)):
            return False
    return True


def _is_true_literal(expr: Expr) -> bool:
    """True only for the boolean literal ``TRUE``, the default that keeps a padded row."""
    return isinstance(expr, exp.Boolean) and expr.this is True


def _present_when_padded(
    sel: exp.Select, *, constrained: frozenset[str], nullable: frozenset[str]
) -> frozenset[str]:
    """Aliases guaranteed present on the rows where every alias in ``constrained`` is
    NULL-padded, so a disjunct referencing one of them genuinely keeps those rows alive.

    A preserved-side alias (never NULL-padded, so absent from ``nullable``) is present on
    every output row. The partner of a single FULL OUTER join is present exactly on the
    rows where the other side is padded, since a full join populates one side per unmatched
    row. An independent optional side carries no such guarantee, so a sibling resting on it
    cannot rescue the drop.
    """
    present = set(_relation_aliases(sel) - nullable)
    if constrained:
        complements = _full_outer_complements(sel)
        common = set(complements.get(next(iter(constrained)), frozenset()))
        for table in constrained:
            common &= complements.get(table, frozenset())
        present |= common
    return frozenset(present)


def _relation_aliases(sel: exp.Select) -> frozenset[str]:
    aliases: set[str] = set()
    from_ = sg.from_of(sel)
    if from_ is not None and from_.this is not None:
        aliases.add(sg.name_of(from_.this))
    for j in sg.joins_of(sel):
        aliases.add(sg.name_of(j.this))
    return frozenset(aliases)


def _full_outer_complements(sel: exp.Select) -> dict[str, frozenset[str]]:
    """The mutually-present partner of a single FULL OUTER join: when one side is padded
    the other is populated. Scoped to a lone full join of two relations, the case where
    the complement is sound; a chain of full joins can leave both sides padded on the same
    row, so anything more complex yields no complement and the detector keeps firing.
    """
    joins = sg.joins_of(sel)
    from_ = sg.from_of(sel)
    if from_ is None or from_.this is None or len(joins) != 1:
        return {}
    if sg.join_side_of(joins[0]) is not JoinSide.FULL:
        return {}
    left, right = sg.name_of(from_.this), sg.name_of(joins[0].this)
    return {left: frozenset({right}), right: frozenset({left})}


def _path(start: Expr, until: Expr) -> Iterator[Expr]:
    """The chain of nodes from ``start`` up to and including ``until``.

    Inclusive of ``until`` so a guard that *is* the boundary node (``GROUP BY
    coalesce(...)``, where the coalesce is the group expression itself) is still seen.
    """
    node: Expr | None = start
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
