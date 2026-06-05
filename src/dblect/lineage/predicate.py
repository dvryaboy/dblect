"""A sound, conservative predicate-implication engine for conditional-fact activation.

A conditional fact holds over the rows matching its predicate ``P``. It activates
at a scope whose accumulated row filter ``F`` *implies* ``P``: then the scope's
rows are a subset of the fact's rows, and a claim that survives row removal (a
candidate key, a ``NOT_NULL``) carries. ``implies(strong, weak)`` decides that
entailment.

It is deliberately partial. ``implies`` returns ``True`` only when it can prove
``strong ⟹ weak`` within a small, totally-decidable fragment: conjunctions of
``term <op> literal`` and ``term IN (...)`` atoms, where ``term`` is a column or a
recognised monotonic bucketing of one (``date_trunc``), and ``op`` is an order
comparison. Reasoning is interval containment on the literals, so a narrower date
bound implies a wider one. Anything outside the fragment (arithmetic, cross-column
atoms, functions we do not model) yields ``False`` rather than a guess: we stay
silent rather than over-claim, the same posture the rest of the audit takes.

The one invariant that must never break is soundness: a ``True`` verdict means
every row satisfying ``strong`` satisfies ``weak``. ``test_predicate_implication``
pins it directly with PBTs that sample concrete worlds across the fragment
(comparisons, ``IN``, ``OR``, truncation terms, and string ordering).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

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
class OpaqueAtom:
    """Anything outside the fragment, keyed by its normalised SQL. It only ever
    matches itself (a bare boolean column against the same column)."""

    sql: str


# A conjunct canonicalised for syntactic matching against ``weak``.
Canon = CmpAtom | InAtom | OpaqueAtom

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
    weak = _unparen(weak)
    if isinstance(weak, exp.And):
        return implies(strong, weak.left) and implies(strong, weak.right)
    if isinstance(weak, exp.Or):
        return implies(strong, weak.left) or implies(strong, weak.right)

    strong = _unparen(strong)
    if isinstance(strong, exp.Or):
        return implies(strong.left, weak) and implies(strong.right, weak)

    conjuncts = _conjuncts(strong)
    weak_canon = _canon(weak)
    if any(_canon(c) == weak_canon for c in conjuncts):
        return True
    cmp_atoms, in_sets = _collect(conjuncts)
    return _entails(cmp_atoms, in_sets, weak)


# --- decomposition ---------------------------------------------------------------


def _unparen(e: Expr) -> Expr:
    while isinstance(e, exp.Paren) and isinstance(e.this, Expr):
        e = e.this
    return e


def _conjuncts(e: Expr) -> list[Expr]:
    e = _unparen(e)
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
    return OpaqueAtom(_unparen(e).sql(dialect="duckdb").lower())


# --- atom recognition ------------------------------------------------------------


def _as_atom(e: Expr) -> CmpAtom | None:
    e = _unparen(e)
    op = _OP_BY_TYPE.get(type(e))
    if op is None:
        return None
    lhs, rhs = e.args.get("this"), e.args.get("expression")
    if not isinstance(lhs, Expr) or not isinstance(rhs, Expr):
        return None
    left, right = _unparen(lhs), _unparen(rhs)
    lterm, llit = _term(left), _lit(left)
    rterm, rlit = _term(right), _lit(right)
    if lterm is not None and rlit is not None and llit is None:
        return CmpAtom(lterm, op, rlit)
    if rterm is not None and llit is not None and rlit is None:
        return CmpAtom(rterm, _FLIP[op], llit)
    return None


def _as_in(e: Expr) -> InAtom | None:
    e = _unparen(e)
    if not isinstance(e, exp.In) or not isinstance(e.this, Expr):
        return None
    term = _term(e.this)
    exprs = e.args.get("expressions")
    if term is None or not exprs:
        return None  # IN (subquery) carries ``query`` not ``expressions``; skip
    vals: set[Lit] = set()
    for x in exprs:
        if not isinstance(x, Expr):
            return None
        v = _lit(x)
        if v is None:
            return None
        vals.add(v)
    return InAtom(term, frozenset(vals))


def _term(e: Expr) -> Term | None:
    """The orderable term of an atom: a column, or ``date_trunc(unit, column)``."""
    e = _unparen(e)
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


def _lit(e: Expr) -> Lit | None:
    e = _unparen(e)
    if isinstance(e, exp.Neg) and isinstance(e.this, Expr):
        inner = _lit(e.this)
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
) -> tuple[dict[Term, list[tuple[Op, Lit]]], dict[Term, frozenset[Lit]]]:
    cmp_atoms: dict[Term, list[tuple[Op, Lit]]] = {}
    in_sets: dict[Term, frozenset[Lit]] = {}
    for c in conjuncts:
        atom = _as_atom(c)
        if atom is not None:
            cmp_atoms.setdefault(atom.term, []).append((atom.op, atom.lit))
            continue
        in_atom = _as_in(c)
        if in_atom is not None:
            prior = in_sets.get(in_atom.term)
            in_sets[in_atom.term] = in_atom.values if prior is None else (prior & in_atom.values)
    return cmp_atoms, in_sets


def _entails(
    cmp_atoms: dict[Term, list[tuple[Op, Lit]]],
    in_sets: dict[Term, frozenset[Lit]],
    weak: Expr,
) -> bool:
    atom = _as_atom(weak)
    if atom is not None:
        if _interval_entails(cmp_atoms.get(atom.term, []), atom.op, atom.lit):
            return True
        in_set = in_sets.get(atom.term)
        return in_set is not None and _set_entails(in_set, atom.op, atom.lit)
    in_atom = _as_in(weak)
    if in_atom is not None:
        have = in_sets.get(in_atom.term)
        if have is not None and have <= in_atom.values:
            return True
        iv = _interval(cmp_atoms.get(in_atom.term, []))
        if iv is not None and _is_point(iv) and iv.lo is not None:
            return Lit(iv.kind, iv.lo) in in_atom.values
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
