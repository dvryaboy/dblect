"""A sound, conservative predicate-implication engine for conditional-fact activation.

A conditional fact holds over the rows matching its predicate ``P``. It activates
at a scope whose accumulated row filter ``F`` *implies* ``P``: then the scope's
rows are a subset of the fact's rows, and a claim that survives row removal (a
candidate key, a ``NOT_NULL``) carries. ``implies(strong, weak)`` decides that
entailment.

It is deliberately partial. ``implies`` returns ``True`` only when it can prove
``strong ⟹ weak`` within a small, totally-decidable fragment: conjunctions of
``term <op> literal``, ``term IN (...)``, and ``term IS NOT NULL`` atoms, where
``term`` is a column or a recognised monotonic bucketing of one (``date_trunc``),
and ``op`` is an order comparison. Reasoning is interval containment on the
literals, so a narrower date bound implies a wider one. Anything outside the
fragment (arithmetic, cross-column atoms, functions we do not model) yields
``False`` rather than a guess: we stay silent rather than over-claim, the same
posture the rest of the audit takes.

The one invariant that must never break is soundness: a ``True`` verdict means
every row satisfying ``strong`` satisfies ``weak``. ``test_predicate_implication``
pins it directly with PBTs that sample concrete worlds across the fragment
(comparisons, ``IN``, ``OR``, truncation terms, and string ordering).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol, cast

import sqlglot
import sqlglot.expressions as exp
from sqlglot import Expr
from sqlglot.errors import SqlglotError


class Op(StrEnum):
    """An order comparison between a term and a literal."""

    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    EQ = "="


class LitKind(StrEnum):
    """The comparison domain of a literal. Values of different kinds are never
    ordered against each other; this tag is what makes that check explicit."""

    NUM = "num"
    STR = "str"


class Orderable(Protocol):
    """A value totally ordered against others *of its own ``LitKind``*: numbers as
    ``Decimal``, strings lexically.

    The ``LitKind`` tag on a :class:`Lit` is what guarantees we never order a number
    against a string; this protocol only states that, within one kind, the order
    comparisons are available. The dunder parameters are ``Any`` because that is how
    a protocol admits both ``Decimal`` and ``str`` (whose own signatures accept only
    their exact type); the kind check carries the runtime contract.
    """

    def __lt__(self, other: Any, /) -> bool: ...
    def __le__(self, other: Any, /) -> bool: ...
    def __gt__(self, other: Any, /) -> bool: ...
    def __ge__(self, other: Any, /) -> bool: ...


@dataclass(frozen=True, slots=True)
class Lit:
    """A literal tagged by its comparison domain. ``value`` is a ``Decimal`` when
    ``kind is NUM`` and a ``str`` when ``kind is STR``."""

    kind: LitKind
    value: Orderable


@dataclass(frozen=True, slots=True)
class Column:
    """A bare column term, case-folded. A table qualifier is dropped: matching a
    predicate column against a renamed downstream column is the lineage layer's job,
    not the engine's."""

    name: str


@dataclass(frozen=True, slots=True)
class Trunc:
    """A recognised monotonic bucketing of a term (``date_trunc(unit, inner)``).
    Keyed structurally by ``unit`` and ``inner`` so ``date_trunc('day', d)`` and
    ``date_trunc('month', d)`` are distinct terms."""

    unit: str
    inner: Term


# An orderable subject of an atom: a column or a monotonic function of one.
Term = Column | Trunc


@dataclass(frozen=True, slots=True)
class CmpAtom:
    """``term <op> literal``."""

    term: Term
    op: Op
    lit: Lit


@dataclass(frozen=True, slots=True)
class InAtom:
    """``term IN (literals...)``."""

    term: Term
    values: frozenset[Lit]


@dataclass(frozen=True, slots=True)
class NotNullAtom:
    """``term IS NOT NULL``."""

    term: Term


@dataclass(frozen=True, slots=True)
class OpaqueAtom:
    """Anything outside the fragment, keyed by its normalised SQL. It only ever
    matches itself (a bare boolean column against the same column)."""

    sql: str


# A conjunct canonicalised for syntactic matching against ``weak``.
Canon = CmpAtom | InAtom | NotNullAtom | OpaqueAtom

_OP_BY_TYPE: dict[type, Op] = {
    exp.GT: Op.GT,
    exp.GTE: Op.GE,
    exp.LT: Op.LT,
    exp.LTE: Op.LE,
    exp.EQ: Op.EQ,
}
# When the literal sits on the left (``5 <= a``), the operator flips to read against
# the term (``a >= 5``).
_FLIP: dict[Op, Op] = {Op.GT: Op.LT, Op.GE: Op.LE, Op.LT: Op.GT, Op.LE: Op.GE, Op.EQ: Op.EQ}


def parse_predicate(sql: str, *, dialect: str = "duckdb") -> Expr | None:
    """Parse a predicate string to a sqlglot expression, or ``None`` if it will not
    parse. ``None`` means "no information" to a caller, never an empty claim."""
    try:
        return sqlglot.parse_one(sql, dialect=dialect)
    except SqlglotError:
        return None


def implies(strong: Expr, weak: Expr) -> bool:
    """``True`` only when ``strong ⟹ weak`` is provable in the supported fragment.

    Boolean structure first: a conjunctive ``weak`` needs every conjunct proven; a
    disjunctive ``weak`` needs one arm; a disjunctive ``strong`` needs every arm to
    prove ``weak``. Then ``strong`` is a conjunction of atoms: ``weak`` holds if it
    matches a conjunct syntactically, or if the conjuncts' interval on ``weak``'s
    term entails it.
    """
    weak = unparen(weak)
    if isinstance(weak, exp.And):
        return implies(strong, weak.left) and implies(strong, weak.right)
    if isinstance(weak, exp.Or):
        return implies(strong, weak.left) or implies(strong, weak.right)

    strong = unparen(strong)
    if isinstance(strong, exp.Or):
        return implies(strong.left, weak) and implies(strong.right, weak)

    conjuncts = _conjuncts(strong)
    weak_canon = _canon(weak)
    if any(_canon(c) == weak_canon for c in conjuncts):
        return True
    cmp_atoms, in_sets, not_null = _collect(conjuncts)
    return _entails(cmp_atoms, in_sets, not_null, weak)


def entailment_checker(strong_atoms: frozenset[Canon]) -> Callable[[frozenset[Canon]], bool]:
    """A tester for "does ``strong_atoms`` entail this weak atom-set", with the strong
    side's interval / ``IN`` constraints folded once.

    The activation step entails many conditional predicates against one relation's
    accumulated filter, so collecting that filter's constraints per predicate is wasted
    work. This folds the strong side once and returns a closure over many weak sets.
    Each weak atom is entailed when ``strong`` carries it verbatim (the syntactic case,
    the only route for an :class:`OpaqueAtom`) or when the collected constraints on its
    term entail it.
    """
    cmp_atoms, in_sets, not_null = _collect_canon(strong_atoms)

    def check(weak_atoms: frozenset[Canon]) -> bool:
        return all(
            w in strong_atoms or _entails_atom(cmp_atoms, in_sets, not_null, w) for w in weak_atoms
        )

    return check


def entails_atoms(strong_atoms: frozenset[Canon], weak_atoms: frozenset[Canon]) -> bool:
    """Whether the conjunction of ``strong_atoms`` implies every atom of ``weak_atoms``.

    The one-shot form of :func:`entailment_checker`, for a caller entailing a single
    weak set.
    """
    return entailment_checker(strong_atoms)(weak_atoms)


# --- atom extraction and column renaming -----------------------------------------
#
# The predicate-flow property carries a relation's accumulated row filter as a set
# of these atoms, so it needs to lift a ``WHERE`` expression into atoms and rename
# an atom's column through a projection. Both operate on the same typed atom forms
# the entailment core reasons about, so they live here rather than re-deriving the
# recognition logic elsewhere.


def atoms_of(e: Expr) -> frozenset[Canon]:
    """The conjuncts of ``e``, each canonicalised to an atom. A conjunct outside the
    fragment (an ``OR``, an unmodelled shape) becomes an :class:`OpaqueAtom`, carried
    but inert to interval reasoning."""
    return frozenset(_canon(c) for c in _conjuncts(e))


def atom_column(atom: Canon) -> str | None:
    """The single base column an atom constrains, or ``None`` for an
    :class:`OpaqueAtom` (whose columns the engine does not model)."""
    if isinstance(atom, CmpAtom | InAtom | NotNullAtom):
        return _term_column(atom.term)
    return None


def rename_atom(
    atom: CmpAtom | InAtom | NotNullAtom, new_column: str
) -> CmpAtom | InAtom | NotNullAtom:
    """``atom`` with its base column replaced by ``new_column`` (renaming the inner
    column of a truncation term), so a filter follows a projection's ``col AS x``."""
    term = _rename_term_column(atom.term, new_column)
    if isinstance(atom, CmpAtom):
        return CmpAtom(term, atom.op, atom.lit)
    if isinstance(atom, InAtom):
        return InAtom(term, atom.values)
    return NotNullAtom(term)


def _term_column(t: Term) -> str:
    return t.name if isinstance(t, Column) else _term_column(t.inner)


def _rename_term_column(t: Term, new_column: str) -> Term:
    if isinstance(t, Column):
        return Column(new_column)
    return Trunc(t.unit, _rename_term_column(t.inner, new_column))


# --- decomposition ---------------------------------------------------------------


def unparen(e: Expr) -> Expr:
    """Unwrap nested ``Paren`` wrappers down to the expression they enclose.
    Public: :mod:`dblect.check.dead_predicate` reuses this to descend from an
    ``exp.Not`` to whatever it negates (possibly parenthesized), rather than
    hand-rolling its own paren-skipping walk."""
    while isinstance(e, exp.Paren) and isinstance(e.this, Expr):
        e = e.this
    return e


def _conjuncts(e: Expr) -> list[Expr]:
    e = unparen(e)
    if isinstance(e, exp.And):
        return _conjuncts(e.left) + _conjuncts(e.right)
    return [e]


def _canon(e: Expr) -> Canon:
    """A structural key for syntactic conjunct matching. Atoms canonicalise to their
    ``(term, op, literal)`` form (so ``5 <= a`` keys like ``a >= 5``); everything
    else falls back to normalised SQL, which still matches a bare boolean column
    against itself."""
    atom = _as_atom(e)
    if atom is not None:
        return atom
    in_atom = _as_in(e)
    if in_atom is not None:
        return in_atom
    not_null_atom = _as_not_null(e)
    if not_null_atom is not None:
        return not_null_atom
    return OpaqueAtom(unparen(e).sql(dialect="duckdb").lower())


# --- atom recognition ------------------------------------------------------------


def _comparison_operands(e: Expr) -> tuple[Expr, Expr] | None:
    """The two unwrapped operand expressions of a binary node, or ``None`` if
    either is missing (defensive: every binary comparison node carries both)."""
    lhs, rhs = e.args.get("this"), e.args.get("expression")
    if not isinstance(lhs, Expr) or not isinstance(rhs, Expr):
        return None
    return unparen(lhs), unparen(rhs)


def _subject_and_literal(e: Expr) -> tuple[Expr, Lit, bool] | None:
    """Split a binary comparison into its non-literal side, the literal from
    the other side, and whether the literal sat on the left (so a caller
    keeping the subject on the left must flip an ordering operator, as
    ``5 <= a`` becomes ``a``/``>=``/``5``). ``None`` when neither or both sides
    are a literal, or a literal side is a bare ``NULL``: every comparison
    operator returns UNKNOWN against NULL, so there is no interval bound or
    membership fact to read off it. Shared by :func:`_as_atom` (whose subject
    can also be a monotonic truncation term) and
    :func:`column_literal_comparison` (whose subject must be a bare column)."""
    operands = _comparison_operands(e)
    if operands is None:
        return None
    left, right = operands
    if isinstance(left, exp.Null) or isinstance(right, exp.Null):
        return None
    llit, rlit = lit_of(left), lit_of(right)
    if rlit is not None and llit is None:
        return left, rlit, False
    if llit is not None and rlit is None:
        return right, llit, True
    return None


def _as_atom(e: Expr) -> CmpAtom | None:
    e = unparen(e)
    op = _OP_BY_TYPE.get(type(e))
    if op is None:
        return None
    split = _subject_and_literal(e)
    if split is None:
        return None
    subject, lit, flipped = split
    term = _term(subject)
    if term is None:
        return None
    return CmpAtom(term, _FLIP[op] if flipped else op, lit)


@dataclass(frozen=True, slots=True)
class ColumnLiteralComparison:
    """``column <comparison> literal``, normalised so the column reads on the
    left (an ordering operator flips when the literal was written first, the
    same normalisation :class:`CmpAtom` applies). ``comparison`` is the
    sqlglot node class actually used, so a caller can recover ``EQ`` vs
    ``NEQ`` vs the null-safe forms, which this module's own five-operator
    :class:`Op` does not distinguish (it has no ``NEQ``: exclusion is not an
    order comparison)."""

    column: exp.Column
    comparison: type[exp.Binary]
    literal: Lit


# The eight comparison operators recognised over a scalar: equality, its
# null-safe form, inequality and its null-safe form, and the four order
# comparisons. Doubling as the type-narrowing step for ``type(e)`` (a bare
# ``type`` from sqlglot's own node), each key maps to itself so a lookup also
# recovers a properly typed ``type[exp.Binary]`` to hand back unflipped.
_COMPARISON_TYPES: dict[type, type[exp.Binary]] = {
    t: t
    for t in (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
}
# Flipped the way an ordering operator reads when the literal was written
# first; equality and inequality (null-safe or not) are symmetric, so they
# flip to themselves.
_COMPARISON_FLIP: dict[type[exp.Binary], type[exp.Binary]] = {
    exp.EQ: exp.EQ,
    exp.NEQ: exp.NEQ,
    exp.NullSafeEQ: exp.NullSafeEQ,
    exp.NullSafeNEQ: exp.NullSafeNEQ,
    exp.LT: exp.GT,
    exp.GT: exp.LT,
    exp.LTE: exp.GTE,
    exp.GTE: exp.LTE,
}


def column_literal_comparison(e: Expr) -> ColumnLiteralComparison | None:
    """``column <comparison> literal`` for the eight comparison operators SQL
    defines over a scalar, normalised so the column reads on the left.
    ``None`` for a column-to-column comparison, a ``NULL`` operand, or any
    node outside the eight (arithmetic, ``LIKE``, ``IS``, an ordering
    comparison against a non-literal, ...).

    Unlike :func:`_as_atom`'s ``Term`` (which also recognises a monotonic
    truncation of a column), the subject here must be a bare ``exp.Column``:
    a caller such as the dead-predicate check needs the actual node to resolve
    a :class:`~dblect.lineage.graph.ColumnRef` through
    :func:`~dblect.lineage.property.resolved_column_ref` and to locate a
    finding's line, neither of which a synthetic ``Term`` carries.
    """
    e = unparen(e)
    kind = _COMPARISON_TYPES.get(type(e))
    if kind is None:
        return None
    split = _subject_and_literal(e)
    if split is None:
        return None
    subject, lit, flipped = split
    if not isinstance(subject, exp.Column):
        return None
    return ColumnLiteralComparison(subject, _COMPARISON_FLIP[kind] if flipped else kind, lit)


def _in_operands(e: Expr) -> tuple[Expr, list[object]] | None:
    """The subject and raw list operands of ``e`` if it is ``term IN
    (...)``, or ``None`` for ``IN (subquery)`` (whose operand lives in
    ``query``, not ``expressions``) or a non-``IN`` node. Shared by
    :func:`_as_in` and :func:`column_in_list`, which differ only in what
    subject shape they accept and how they treat a ``NULL`` member."""
    e = unparen(e)
    if not isinstance(e, exp.In) or not isinstance(e.this, Expr):
        return None
    exprs = e.args.get("expressions")
    if not exprs:
        return None
    return e.this, cast("list[object]", exprs)


def _as_in(e: Expr) -> InAtom | None:
    operands = _in_operands(e)
    if operands is None:
        return None
    subject, exprs = operands
    term = _term(subject)
    if term is None:
        return None
    vals: set[Lit] = set()
    for x in exprs:
        if not isinstance(x, Expr):
            return None
        v = lit_of(x)
        if v is None:
            return None
        vals.add(v)
    return InAtom(term, frozenset(vals))


@dataclass(frozen=True, slots=True)
class ColumnInList:
    """``column IN (literals...)``, or a ``NOT IN`` list a caller inspects the
    same way (this module makes no claim about negation; that reading is the
    caller's boolean structure, not this atom's). ``has_null`` flags a bare
    ``NULL`` member: SQL's ``NOT IN`` never matches when the list carries a
    NULL, a different hazard from a stray literal, so a caller needs to tell
    the two apart rather than have the member silently dropped."""

    column: exp.Column
    literals: frozenset[Lit]
    has_null: bool


def column_in_list(e: Expr) -> ColumnInList | None:
    """``column IN (literals...)`` with a bare column subject (the
    equality-family sibling of :func:`column_literal_comparison`). ``None``
    for ``IN (subquery)``, a non-column subject, or a member that is neither a
    literal nor a bare ``NULL``: dropping just that member would silently
    understate the list, so the whole match fails instead."""
    operands = _in_operands(e)
    if operands is None:
        return None
    subject, exprs = operands
    subject = unparen(subject)
    if not isinstance(subject, exp.Column):
        return None
    literals: set[Lit] = set()
    has_null = False
    for x in exprs:
        if not isinstance(x, Expr):
            return None
        member = unparen(x)
        if isinstance(member, exp.Null):
            has_null = True
            continue
        lit = lit_of(member)
        if lit is None:
            return None
        literals.add(lit)
    return ColumnInList(subject, frozenset(literals), has_null)


def _as_not_null(e: Expr) -> NotNullAtom | None:
    e = unparen(e)
    if not isinstance(e, exp.Not):
        return None
    inner = e.this
    if not isinstance(inner, Expr):
        return None
    inner = unparen(inner)
    if not isinstance(inner, exp.Is):
        return None
    rhs = inner.args.get("expression")
    if not isinstance(rhs, exp.Null):
        return None
    lhs = inner.args.get("this")
    if not isinstance(lhs, Expr):
        return None
    term = _term(lhs)
    if term is None:
        return None
    return NotNullAtom(term)


def _term(e: Expr) -> Term | None:
    """The orderable term of an atom: a column, or ``date_trunc(unit, column)``."""
    e = unparen(e)
    if isinstance(e, exp.Column):
        return Column(e.name.lower())
    # duckdb compiles ``date_trunc(unit, col)`` to TimestampTrunc; other dialects
    # use DateTrunc. Both are monotonic non-decreasing in the column, so a bound on
    # the truncation participates in interval reasoning under its own term key.
    if isinstance(e, exp.TimestampTrunc | exp.DateTrunc):
        inner = _term(e.this) if isinstance(e.this, Expr) else None
        unit = _unit_text(e.args.get("unit"))
        if inner is not None and unit is not None:
            return Trunc(unit, inner)
    return None


def _unit_text(unit: object) -> str | None:
    if isinstance(unit, exp.Literal):
        return unit.this.lower()
    if isinstance(unit, exp.Var | exp.Column):
        return unit.name.lower()
    return None


def lit_of(e: Expr) -> Lit | None:
    """The :class:`Lit` a literal expression denotes, or ``None`` when ``e`` is
    not a literal (or is numeric text this engine cannot parse). Public: the
    value-domain property reuses this to turn a SQL literal into the same
    typed value this module's atoms carry, so ``1`` and ``'1'`` stay distinct
    there too."""
    e = unparen(e)
    if isinstance(e, exp.Neg) and isinstance(e.this, Expr):
        inner = lit_of(e.this)
        if inner is not None and inner.kind is LitKind.NUM and isinstance(inner.value, Decimal):
            return Lit(LitKind.NUM, -inner.value)
        return None
    if isinstance(e, exp.Literal):
        if e.is_string:
            return Lit(LitKind.STR, e.this)
        try:
            return Lit(LitKind.NUM, Decimal(e.this))
        except InvalidOperation:
            return None
    return None


# --- entailment over collected constraints ---------------------------------------


def _collect(
    conjuncts: list[Expr],
) -> tuple[dict[Term, list[tuple[Op, Lit]]], dict[Term, frozenset[Lit]], set[Term]]:
    cmp_atoms: dict[Term, list[tuple[Op, Lit]]] = {}
    in_sets: dict[Term, frozenset[Lit]] = {}
    not_null: set[Term] = set()
    for c in conjuncts:
        atom = _as_atom(c)
        if atom is not None:
            cmp_atoms.setdefault(atom.term, []).append((atom.op, atom.lit))
            continue
        in_atom = _as_in(c)
        if in_atom is not None:
            prior = in_sets.get(in_atom.term)
            in_sets[in_atom.term] = in_atom.values if prior is None else (prior & in_atom.values)
            continue
        not_null_atom = _as_not_null(c)
        if not_null_atom is not None:
            not_null.add(not_null_atom.term)
    return cmp_atoms, in_sets, not_null


def _collect_canon(
    atoms: frozenset[Canon],
) -> tuple[dict[Term, list[tuple[Op, Lit]]], dict[Term, frozenset[Lit]], set[Term]]:
    """The same fold as :func:`_collect`, over already-canonicalised atoms. An
    :class:`OpaqueAtom` contributes nothing to interval reasoning; it is matched only
    syntactically by the caller."""
    cmp_atoms: dict[Term, list[tuple[Op, Lit]]] = {}
    in_sets: dict[Term, frozenset[Lit]] = {}
    not_null: set[Term] = set()
    for atom in atoms:
        if isinstance(atom, CmpAtom):
            cmp_atoms.setdefault(atom.term, []).append((atom.op, atom.lit))
        elif isinstance(atom, InAtom):
            prior = in_sets.get(atom.term)
            in_sets[atom.term] = atom.values if prior is None else (prior & atom.values)
        elif isinstance(atom, NotNullAtom):
            not_null.add(atom.term)
    return cmp_atoms, in_sets, not_null


def _entails(
    cmp_atoms: dict[Term, list[tuple[Op, Lit]]],
    in_sets: dict[Term, frozenset[Lit]],
    not_null: set[Term],
    weak: Expr,
) -> bool:
    atom = _as_atom(weak) or _as_in(weak) or _as_not_null(weak)
    return atom is not None and _entails_atom(cmp_atoms, in_sets, not_null, atom)


def _entails_atom(
    cmp_atoms: dict[Term, list[tuple[Op, Lit]]],
    in_sets: dict[Term, frozenset[Lit]],
    not_null: set[Term],
    weak: Canon,
) -> bool:
    """Whether the collected constraints entail one canonical ``weak`` atom. An
    :class:`OpaqueAtom` is never entailed here; it only matches by membership."""
    if isinstance(weak, CmpAtom):
        if _interval_entails(cmp_atoms.get(weak.term, []), weak.op, weak.lit):
            return True
        in_set = in_sets.get(weak.term)
        return in_set is not None and _set_entails(in_set, weak.op, weak.lit)
    if isinstance(weak, InAtom):
        have = in_sets.get(weak.term)
        if have is not None and have <= weak.values:
            return True
        iv = _interval(cmp_atoms.get(weak.term, []))
        return (
            iv is not None
            and _is_point(iv)
            and iv.lo is not None
            and Lit(iv.kind, iv.lo) in weak.values
        )
    if isinstance(weak, NotNullAtom):
        # A comparison or IN never evaluates true against NULL, so a row that passed
        # it is provably non-null on that same term; ditto an explicit NOT NULL atom.
        return weak.term in cmp_atoms or weak.term in in_sets or weak.term in not_null
    return False


@dataclass(frozen=True, slots=True)
class Interval:
    """The bounds a term is pinned to by the collected comparison atoms. A ``None``
    bound is unbounded on that side."""

    kind: LitKind
    lo: Orderable | None
    lo_incl: bool
    hi: Orderable | None
    hi_incl: bool


def _interval(atoms: list[tuple[Op, Lit]]) -> Interval | None:
    """Fold same-kind comparison atoms on one term into an interval, or ``None`` if
    the term carries mixed literal kinds (incomparable, so unusable)."""
    if not atoms:
        return None
    kinds = {lit.kind for _op, lit in atoms}
    if len(kinds) != 1:
        return None
    kind = next(iter(kinds))
    lo: Orderable | None = None
    hi: Orderable | None = None
    lo_incl = hi_incl = True
    for op, lit in atoms:
        v = lit.value
        if op in (Op.GT, Op.GE):
            incl = op is Op.GE
            if lo is None or v > lo or (v == lo and lo_incl and not incl):
                lo, lo_incl = v, incl
        elif op in (Op.LT, Op.LE):
            incl = op is Op.LE
            if hi is None or v < hi or (v == hi and hi_incl and not incl):
                hi, hi_incl = v, incl
        else:  # Op.EQ
            lo = hi = v
            lo_incl = hi_incl = True
    return Interval(kind, lo, lo_incl, hi, hi_incl)


def _is_empty(iv: Interval) -> bool:
    if iv.lo is None or iv.hi is None:
        return False
    return iv.lo > iv.hi or (iv.lo == iv.hi and not (iv.lo_incl and iv.hi_incl))


def _is_point(iv: Interval) -> bool:
    return iv.lo is not None and iv.lo == iv.hi and iv.lo_incl and iv.hi_incl


def _interval_entails(atoms: list[tuple[Op, Lit]], op: Op, lit: Lit) -> bool:
    iv = _interval(atoms)
    if iv is None:
        return False
    if _is_empty(iv):
        return True  # an unsatisfiable strong implies anything (vacuously)
    if iv.kind != lit.kind:
        return False
    lo, hi, wv = iv.lo, iv.hi, lit.value
    # A non-strict weak bound (>=, <=) needs only that the strong bound reaches it;
    # the strong bound's own strictness does not matter. A strict weak bound (>, <)
    # additionally rules out equality unless the strong bound already excludes it.
    if op is Op.GE:
        return lo is not None and lo >= wv
    if op is Op.GT:
        return lo is not None and (lo > wv or (lo == wv and not iv.lo_incl))
    if op is Op.LE:
        return hi is not None and hi <= wv
    if op is Op.LT:
        return hi is not None and (hi < wv or (hi == wv and not iv.hi_incl))
    return lo is not None and lo == hi == wv and iv.lo_incl and iv.hi_incl  # Op.EQ


def _set_entails(in_set: frozenset[Lit], op: Op, lit: Lit) -> bool:
    return bool(in_set) and all(x.kind == lit.kind and _cmp(x.value, op, lit.value) for x in in_set)


def _cmp(x: Orderable, op: Op, y: Orderable) -> bool:
    if op is Op.LT:
        return x < y
    if op is Op.LE:
        return x <= y
    if op is Op.GT:
        return x > y
    if op is Op.GE:
        return x >= y
    return x == y  # Op.EQ
