"""Dead predicate over a closed value set: the pure decision procedure.

A ``NominalEnum``/``UnitEnum`` field, or a dbt ``accepted_values`` test, names
the closed set a column's value is drawn from (the propagated ``ValueDomain``,
:mod:`dblect.lineage.properties.value_domain`). A literal compared against that
column outside the set is provably dead; a ``CASE`` remap that silently drops a
member to a non-null default is a coverage gap. This module reads the raw AST
against a small decision table declared as data; :mod:`dblect.check.run` wires
its verdicts into located :class:`~dblect.check.findings.CheckFinding` objects.
The domain covers non-null values only, so every verdict starts three-valued
(FALSE / TRUE / UNKNOWN on a NULL row) and collapses only where SQL itself
does: a filtering context (``WHERE``/``JOIN ... ON``/a ``CASE WHEN`` condition)
reads UNKNOWN as FALSE, so "never true" becomes "always empty" or "never
taken" there, while a projected scalar stays "never true".
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import cast

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.check.findings import CheckFindingKind
from dblect.lineage.graph import ColumnRef
from dblect.lineage.predicate import (
    Lit,
    LitKind,
    column_in_list,
    column_literal_comparison,
    lit_of,
    unparen,
)
from dblect.lineage.properties.value_domain import Bounded, ValueDomain
from dblect.lineage.property import resolved_column_ref

# --- literal classification --------------------------------------------------


class LiteralClass(StrEnum):
    """How one literal operand relates to a column's declared or propagated
    :class:`ValueDomain`, decided by exact value first, then letter case, then
    whether the literal's kind (string or numeric) appears in the set at all."""

    MEMBER = auto()
    """An exact member: same kind, same value. Silent."""
    NON_MEMBER = auto()
    """A stray value of the same kind the domain otherwise carries (or the
    domain is the empty set, which every value is a stray against)."""
    CASE_ONLY_MISMATCH = auto()
    """A string that matches a member's text under case-folding but not
    exactly: dead only under a case-sensitive collation."""
    KIND_MISMATCH = auto()
    """The literal's kind (string/numeric) is not one the domain carries at
    all: very likely a different bug (the wrong column), so left silent."""


def classify_literal(domain: ValueDomain, lit: Lit) -> LiteralClass | None:
    """``lit``'s :class:`LiteralClass` against ``domain``, or ``None`` when
    ``domain`` is ``Unbounded`` (no verdict is possible)."""
    if not isinstance(domain, Bounded):
        return None
    if lit in domain.values:
        return LiteralClass.MEMBER
    if lit.kind is LitKind.STR and isinstance(lit.value, str):
        folded = lit.value.casefold()
        if any(
            v.kind is LitKind.STR and isinstance(v.value, str) and v.value.casefold() == folded
            for v in domain.values
        ):
            return LiteralClass.CASE_ONLY_MISMATCH
    if not domain.values or any(v.kind == lit.kind for v in domain.values):
        return LiteralClass.NON_MEMBER
    return LiteralClass.KIND_MISMATCH


# --- comparison form ----------------------------------------------------------


class CompForm(StrEnum):
    """The closed set of comparison shapes this check reads. A declared set
    supports equality only, so ordering comparisons, ``LIKE``, and ``IS`` are
    never scanned into this type at all rather than scanned and then decided
    silent."""

    EQ = auto()
    NULL_SAFE_EQ = auto()  # IS NOT DISTINCT FROM: no UNKNOWN case, constant FALSE/TRUE outright
    NEQ = auto()
    NULL_SAFE_NEQ = auto()  # IS DISTINCT FROM
    IN = auto()
    NOT_IN = auto()


# The form axis of the decision table: dead-family forms (stray means dead) vs.
# the rest (stray means redundant), and the null-safe forms (no UNKNOWN case).
_DEAD_FAMILY: frozenset[CompForm] = frozenset({CompForm.EQ, CompForm.NULL_SAFE_EQ, CompForm.IN})
_NULL_SAFE: frozenset[CompForm] = frozenset({CompForm.NULL_SAFE_EQ, CompForm.NULL_SAFE_NEQ})


# --- boolean context -----------------------------------------------------------


class BoolContext(StrEnum):
    """Where a comparison sits, found by walking up from it to its governing
    boolean structure. Fixes the wording (and, for ``PROJECTED``, whether
    ``REDUNDANT_PREDICATE`` can fire at all: a projected value is not
    filtering anything, so "redundant" has no meaning there)."""

    WHERE_LIKE = auto()  # a top-level WHERE / HAVING / QUALIFY conjunct
    JOIN_ON = auto()  # a JOIN ... ON conjunct
    OR_ARM = auto()  # a disjunct of an OR
    CASE_WHEN = auto()  # a CASE WHEN condition
    PROJECTED = auto()  # anything else: a bare projected scalar or nested expression


def boolean_context(atom_root: Expr) -> tuple[BoolContext, bool]:
    """The governing :class:`BoolContext` for ``atom_root`` (the comparison, or
    for ``NOT IN`` the enclosing ``NOT``), and whether a further ``NOT``
    ancestor flips its polarity (``NOT (col = 'stray')`` reads redundant, not
    dead). ``AND``/``Paren`` pass through unchanged; ``OR`` and a governing
    clause are both terminal, so the walk stops at the first one reached."""
    negated = False
    current = atom_root
    while True:
        parent = current.parent
        if parent is None:
            return BoolContext.PROJECTED, negated
        if isinstance(parent, exp.Paren):
            current = parent
            continue
        if isinstance(parent, exp.Not):
            negated = not negated
            current = parent
            continue
        if isinstance(parent, exp.And):
            current = parent
            continue
        if isinstance(parent, exp.Or):
            return BoolContext.OR_ARM, negated
        if isinstance(parent, exp.If) and current.arg_key == "this":
            return BoolContext.CASE_WHEN, negated
        if isinstance(parent, exp.Join) and current.arg_key == "on":
            return BoolContext.JOIN_ON, negated
        if isinstance(parent, exp.Where | exp.Having | exp.Qualify):
            return BoolContext.WHERE_LIKE, negated
        # A projection, a function argument, an ELSE/THEN slot, a subquery
        # boundary, ...: none of these are filtering contexts.
        return BoolContext.PROJECTED, negated


# --- the decision table: context x dead-flag -> kind and wording -------------


@dataclass(frozen=True, slots=True)
class _Wording:
    """One row's two cells: the wording when a literal makes the comparison
    dead, and when it makes it merely redundant (``None`` where "redundant"
    carries no meaning, read by :func:`_wording` as silent)."""

    dead: str
    live: str | None


# One row per BoolContext; a null-safe form ignores context (no UNKNOWN case)
# and reads the row below instead.
_CONTEXT_WORDING: Mapping[BoolContext, _Wording] = {
    BoolContext.WHERE_LIKE: _Wording("the result is always empty", "filters only NULL rows"),
    BoolContext.JOIN_ON: _Wording("never matches", "matches every non-null pairing"),
    BoolContext.OR_ARM: _Wording(
        "this disjunct never matches", "this disjunct is true on every non-null row"
    ),
    BoolContext.CASE_WHEN: _Wording(
        "this arm is never taken", "this arm's condition holds on every non-null row"
    ),
    BoolContext.PROJECTED: _Wording("never true", None),
}
_NULL_SAFE_WORDING = _Wording("is constant FALSE", "is constant TRUE")

_KIND_BY_DEAD: Mapping[bool, CheckFindingKind] = {
    True: CheckFindingKind.DEAD_PREDICATE,
    False: CheckFindingKind.REDUNDANT_PREDICATE,
}


def _wording(form: CompForm, context: BoolContext, *, dead: bool) -> str | None:
    row = _NULL_SAFE_WORDING if form in _NULL_SAFE else _CONTEXT_WORDING[context]
    return row.dead if dead else row.live


# --- the verdict ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    """One finding this check's decision procedure reached: which kind, the
    message naming what was compared and why, and the column it is about (for
    the report, and for ``-- noqa`` line/column matching)."""

    kind: CheckFindingKind
    message: str
    column: str


def _literal_display(lit: Lit) -> str:
    return lit.value if lit.kind is LitKind.STR and isinstance(lit.value, str) else str(lit.value)


def atom_verdict(
    form: CompForm,
    domain: ValueDomain,
    lit: Lit,
    context: BoolContext,
    *,
    column: str,
    negated: bool,
) -> Verdict | None:
    """The verdict for one comparison's literal operand, reading the decision
    table above; ``None`` when the literal class or context leaves this
    silent. ``column`` is carried through to the result rather than inspected
    here."""
    literal_class = classify_literal(domain, lit)
    if literal_class in (None, LiteralClass.KIND_MISMATCH, LiteralClass.MEMBER):
        return None
    if literal_class is LiteralClass.CASE_ONLY_MISMATCH:
        wording = _wording(form, context, dead=True)
        return Verdict(
            kind=CheckFindingKind.DEAD_PREDICATE_CASE_ONLY,
            message=(
                f"{_literal_display(lit)!r} matches a declared value only by case; "
                f"whether it {wording} depends on the warehouse's collation"
            ),
            column=column,
        )
    # NON_MEMBER: which family the comparison belongs to, flipped once per NOT
    # ancestor the boolean-context walk crossed.
    is_dead = (form in _DEAD_FAMILY) != negated
    wording = _wording(form, context, dead=is_dead)
    if wording is None:
        return None  # a projected value filters nothing; "redundant" has no meaning here
    return Verdict(
        kind=_KIND_BY_DEAD[is_dead],
        message=f"{_literal_display(lit)!r} is not among the declared values; {wording}",
        column=column,
    )


# --- CASE coverage --------------------------------------------------------------


def _case_arm_membership(cond: Expr) -> tuple[ColumnRef, frozenset[Lit]] | None:
    """``col = lit`` or ``col IN (lit, ...)``, unwrapped (no ``NOT``, no ``OR``),
    or ``None`` when ``cond`` is any other shape. A ``NULL`` member of an
    ``IN`` list makes coverage undecidable here (unlike the top-level scan,
    which tracks it), so it is treated the same as any other unresolved shape."""
    comparison = column_literal_comparison(cond)
    if comparison is not None and comparison.comparison is exp.EQ:
        ref = resolved_column_ref(comparison.column)
        return (ref, frozenset({comparison.literal})) if ref is not None else None
    in_list = column_in_list(cond)
    if in_list is not None and not in_list.has_null:
        ref = resolved_column_ref(in_list.column)
        return (ref, in_list.literals) if ref is not None else None
    return None


def _arm_subject_and_literals(case: exp.Case) -> tuple[ColumnRef, frozenset[Lit]] | None:
    """The one column every arm tests and the union of its WHEN literals, or
    ``None`` when coverage is undecidable: an arm of another shape, or arms
    that disagree on which column they test."""
    ifs = cast("list[exp.If]", case.args.get("ifs") or [])
    if not ifs:
        return None
    subject = case.args.get("this")
    column: ColumnRef | None = None
    literals: set[Lit] = set()
    if subject is not None:
        if not isinstance(subject, exp.Column):
            return None
        column = resolved_column_ref(subject)
        if column is None:
            return None
        for arm in ifs:
            when = arm.args.get("this")
            lit = lit_of(when) if isinstance(when, Expr) else None
            if lit is None:
                return None
            literals.add(lit)
        return column, frozenset(literals)
    for arm in ifs:
        when = arm.args.get("this")
        if not isinstance(when, Expr):
            return None
        parsed = _case_arm_membership(when)
        if parsed is None:
            return None
        arm_column, arm_literals = parsed
        if column is None:
            column = arm_column
        elif column != arm_column:
            return None
        literals.update(arm_literals)
    return (column, frozenset(literals)) if column is not None else None


def _is_indicator_idiom(case: exp.Case) -> bool:
    """The shape to spare: a single-arm CASE, or a disjunction indicator whose
    every THEN literal (a bare ``NULL`` THEN ignored) is the same literal
    (``sum(case when status = 'a' then 1 when status = 'b' then 1 else 0
    end)``). Either shape computes something *about* the column rather than
    remapping it. Whether a THEN literal lies inside or outside the column's
    domain plays no part: a multi-arm remap into a fresh vocabulary is exactly
    what ``CASE_LEAVES_ENUM_MEMBER_UNHANDLED`` exists to catch."""
    ifs = cast("list[exp.If]", case.args.get("ifs") or [])
    if len(ifs) == 1:
        return True
    thens: list[Lit] = []
    for arm in ifs:
        then = arm.args.get("true")
        if not isinstance(then, Expr):
            return False
        if isinstance(then, exp.Null):
            continue
        lit = lit_of(then)
        if lit is None:
            return False
        thens.append(lit)
    return bool(thens) and len(set(thens)) == 1


def _default_is_absent_null_or_literal(case: exp.Case) -> bool:
    default = case.args.get("default")
    if default is None:
        return True
    if not isinstance(default, Expr):
        return False
    default = unparen(default)
    return isinstance(default, exp.Null) or lit_of(default) is not None


def case_coverage_verdict(
    case: exp.Case, domain_of: Callable[[ColumnRef], ValueDomain]
) -> Verdict | None:
    """The coverage verdict for one ``CASE``: fires when a multi-arm remap
    leaves a real domain member unhandled, silently routed to the default
    (absent, ``ELSE NULL``, or a literal ``ELSE``). Any other default (``ELSE
    status`` passes the member through unchanged, ``ELSE other_col`` is not a
    place a member falls into) leaves coverage undecidable, so it is silent."""
    if not _default_is_absent_null_or_literal(case):
        return None
    parsed = _arm_subject_and_literals(case)
    if parsed is None:
        return None
    column, covered = parsed
    domain = domain_of(column)
    if not isinstance(domain, Bounded) or not domain.values:
        return None
    if _is_indicator_idiom(case):
        return None
    missing = domain.values - covered
    if not missing:
        return None
    names = ", ".join(repr(_literal_display(m)) for m in sorted(missing, key=_literal_display))
    return Verdict(
        kind=CheckFindingKind.CASE_LEAVES_ENUM_MEMBER_UNHANDLED,
        message=(
            f"this CASE over {column.column!r} does not handle {names}; "
            "unhandled members fall silently to the default"
        ),
        column=column.column,
    )


# --- reading one statement tree ------------------------------------------------

# CompForm derives straight from the sqlglot node class column_literal_comparison
# hands back; only the equality family is scanned for (a declared set supports
# equality alone, per CompForm's own docstring), so LT/LE/GT/GE never appear here.
_COMP_FORM_BY_TYPE: dict[type[exp.Binary], CompForm] = {
    exp.EQ: CompForm.EQ,
    exp.NullSafeEQ: CompForm.NULL_SAFE_EQ,
    exp.NEQ: CompForm.NEQ,
    exp.NullSafeNEQ: CompForm.NULL_SAFE_NEQ,
}


def _not_wrapped_in_nodes(tree: Expr) -> dict[int, exp.Not]:
    """Every ``exp.In`` node that sits directly under a ``NOT`` (through zero
    or more ``Paren``s), keyed by ``id`` of the ``In`` node, mapped to the
    ``Not`` that negates it. Descends from each ``Not`` via ``unparen`` rather
    than climbing from the ``In`` node up: sqlglot parses ``NOT (col IN
    (...))`` as ``Not(this=Paren(this=In(...)))``, so unwrapping downward
    reaches the same ``In`` node an ascending walk would."""
    out: dict[int, exp.Not] = {}
    for not_node in tree.find_all(exp.Not):
        inner = not_node.this
        if isinstance(inner, Expr):
            candidate = unparen(inner)
            if isinstance(candidate, exp.In):
                out[id(candidate)] = not_node
    return out


def dead_predicate_verdicts(
    tree: Expr, domain_of: Callable[[ColumnRef], ValueDomain]
) -> list[tuple[Expr, Verdict]]:
    """Every dead/redundant/case-only/coverage verdict in one parsed statement
    tree, each paired with the AST node a caller locates a line from."""
    out: list[tuple[Expr, Verdict]] = []
    for node in tree.find_all(*_COMP_FORM_BY_TYPE):
        comparison = column_literal_comparison(node)
        if comparison is None:
            continue
        ref = resolved_column_ref(comparison.column)
        if ref is None:
            continue
        context, negated = boolean_context(node)
        verdict = atom_verdict(
            _COMP_FORM_BY_TYPE[comparison.comparison],
            domain_of(ref),
            comparison.literal,
            context,
            column=ref.column,
            negated=negated,
        )
        if verdict is not None:
            out.append((node, verdict))

    not_wrapped = _not_wrapped_in_nodes(tree)
    for node in tree.find_all(exp.In):
        in_list = column_in_list(node)
        if in_list is None:
            continue
        ref = resolved_column_ref(in_list.column)
        if ref is None:
            continue
        negating_not = not_wrapped.get(id(node))
        if negating_not is not None:
            if in_list.has_null:
                # A NULL element already makes NOT IN dead on its own, a
                # different hazard this check leaves alone.
                continue
            form, root = CompForm.NOT_IN, negating_not
        else:
            form, root = CompForm.IN, node
        context, negated = boolean_context(root)
        domain = domain_of(ref)
        for lit in in_list.literals:
            verdict = atom_verdict(form, domain, lit, context, column=ref.column, negated=negated)
            if verdict is not None:
                out.append((node, verdict))

    for case in tree.find_all(exp.Case):
        verdict = case_coverage_verdict(case, domain_of)
        if verdict is not None:
            out.append((case, verdict))
    return out
