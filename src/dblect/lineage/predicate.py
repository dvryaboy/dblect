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
pins it directly with a PBT that samples concrete worlds.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import sqlglot
import sqlglot.expressions as exp
from sqlglot import Expr
from sqlglot.errors import SqlglotError

# A literal tagged by kind so a number is never ordered against a string. The kind
# tag (checked before every comparison) carries the soundness; the value is typed
# ``Any`` because a ``Decimal | str`` union is not statically orderable, and the
# soundness PBT guards the runtime contract.
_Lit = tuple[str, Any]  # ("num", Decimal) | ("str", str)
# A term is a column or a recognised monotonic function of one, keyed structurally
# so ``date_trunc('day', d)`` and ``date_trunc('month', d)`` are distinct terms.
_Term = tuple[object, ...]
_Atom = tuple[_Term, str, _Lit]  # (term, op, literal); op in <,<=,>,>=,=

_TERM_LEFT: dict[type, str] = {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=", exp.EQ: "="}
_TERM_RIGHT: dict[type, str] = {exp.GT: "<", exp.GTE: "<=", exp.LT: ">", exp.LTE: ">=", exp.EQ: "="}


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


def _canon(e: Expr) -> tuple[object, ...]:
    """A structural key for syntactic conjunct matching. Atoms canonicalise to
    their ``(term, op, literal)`` form (so ``5 <= a`` keys like ``a >= 5``);
    everything else falls back to normalised SQL, which still matches a bare
    boolean column against itself."""
    atom = _as_atom(e)
    if atom is not None:
        return ("atom", *atom)
    in_atom = _as_in(e)
    if in_atom is not None:
        return ("in", in_atom[0], in_atom[1])
    return ("sql", _unparen(e).sql(dialect="duckdb").lower())


# --- atom recognition ------------------------------------------------------------


def _as_atom(e: Expr) -> _Atom | None:
    e = _unparen(e)
    op_left = _TERM_LEFT.get(type(e))
    if op_left is None:
        return None
    lhs, rhs = e.args.get("this"), e.args.get("expression")
    if not isinstance(lhs, Expr) or not isinstance(rhs, Expr):
        return None
    left, right = _unparen(lhs), _unparen(rhs)
    lterm, llit = _term(left), _lit(left)
    rterm, rlit = _term(right), _lit(right)
    if lterm is not None and rlit is not None and llit is None:
        return (lterm, op_left, rlit)
    if rterm is not None and llit is not None and rlit is None:
        return (rterm, _TERM_RIGHT[type(e)], llit)
    return None


def _as_in(e: Expr) -> tuple[_Term, frozenset[_Lit]] | None:
    e = _unparen(e)
    if not isinstance(e, exp.In) or not isinstance(e.this, Expr):
        return None
    term = _term(e.this)
    exprs = e.args.get("expressions")
    if term is None or not exprs:
        return None  # IN (subquery) carries ``query`` not ``expressions``; skip
    vals: set[_Lit] = set()
    for x in exprs:
        if not isinstance(x, Expr):
            return None
        v = _lit(x)
        if v is None:
            return None
        vals.add(v)
    return (term, frozenset(vals))


def _term(e: Expr) -> _Term | None:
    """The orderable term of an atom: a column, or ``date_trunc(unit, column)``.

    A table qualifier is dropped here; matching a predicate column against a
    renamed downstream column is the lineage layer's job, not the engine's."""
    e = _unparen(e)
    if isinstance(e, exp.Column):
        return ("col", e.name.lower())
    # duckdb compiles ``date_trunc(unit, col)`` to TimestampTrunc; other dialects
    # use DateTrunc. Both are monotonic non-decreasing in the column, so a bound on
    # the truncation participates in interval reasoning under its own term key.
    if isinstance(e, exp.TimestampTrunc | exp.DateTrunc):
        inner = _term(e.this) if isinstance(e.this, Expr) else None
        unit = _unit_text(e.args.get("unit"))
        if inner is not None and unit is not None:
            return ("trunc", unit, inner)
    return None


def _unit_text(unit: object) -> str | None:
    if isinstance(unit, exp.Literal):
        return unit.this.lower()
    if isinstance(unit, exp.Var | exp.Column):
        return unit.name.lower()
    return None


def _lit(e: Expr) -> _Lit | None:
    e = _unparen(e)
    if isinstance(e, exp.Neg):
        inner = _lit(e.this) if isinstance(e.this, Expr) else None
        return ("num", -inner[1]) if inner is not None and inner[0] == "num" else None
    if isinstance(e, exp.Literal):
        if e.is_string:
            return ("str", e.this)
        try:
            return ("num", Decimal(e.this))
        except InvalidOperation:
            return None
    return None


# --- entailment over collected constraints ---------------------------------------


def _collect(
    conjuncts: list[Expr],
) -> tuple[dict[_Term, list[tuple[str, _Lit]]], dict[_Term, frozenset[_Lit]]]:
    cmp_atoms: dict[_Term, list[tuple[str, _Lit]]] = {}
    in_sets: dict[_Term, frozenset[_Lit]] = {}
    for c in conjuncts:
        atom = _as_atom(c)
        if atom is not None:
            term, op, lit = atom
            cmp_atoms.setdefault(term, []).append((op, lit))
            continue
        in_atom = _as_in(c)
        if in_atom is not None:
            term, vals = in_atom
            in_sets[term] = vals if term not in in_sets else (in_sets[term] & vals)
    return cmp_atoms, in_sets


def _entails(
    cmp_atoms: dict[_Term, list[tuple[str, _Lit]]],
    in_sets: dict[_Term, frozenset[_Lit]],
    weak: Expr,
) -> bool:
    atom = _as_atom(weak)
    if atom is not None:
        term, op, lit = atom
        if _interval_entails(cmp_atoms.get(term, []), op, lit):
            return True
        in_set = in_sets.get(term)
        return in_set is not None and _set_entails(in_set, op, lit)
    in_atom = _as_in(weak)
    if in_atom is not None:
        term, want = in_atom
        have = in_sets.get(term)
        if have is not None and have <= want:
            return True
        iv = _interval(cmp_atoms.get(term, []))
        return iv is not None and _is_point(iv) and (iv[0], iv[1]) in want
    return False


# An interval is (kind, lo, lo_incl, hi, hi_incl); a None bound is unbounded.
_Interval = tuple[str, Any, bool, Any, bool]


def _interval(atoms: list[tuple[str, _Lit]]) -> _Interval | None:
    """Fold same-kind comparison atoms on one term into an interval, or ``None`` if
    the term carries mixed literal kinds (incomparable, so unusable)."""
    if not atoms:
        return None
    kinds = {lit[0] for _op, lit in atoms}
    if len(kinds) != 1:
        return None
    kind = next(iter(kinds))
    lo: Any = None
    hi: Any = None
    lo_incl = hi_incl = True
    for op, (_k, v) in atoms:
        if op in (">", ">="):
            incl = op == ">="
            if lo is None or v > lo or (v == lo and lo_incl and not incl):
                lo, lo_incl = v, incl
        elif op in ("<", "<="):
            incl = op == "<="
            if hi is None or v < hi or (v == hi and hi_incl and not incl):
                hi, hi_incl = v, incl
        else:  # "="
            lo = hi = v
            lo_incl = hi_incl = True
    return (kind, lo, lo_incl, hi, hi_incl)


def _is_empty(iv: _Interval) -> bool:
    _kind, lo, lo_incl, hi, hi_incl = iv
    if lo is None or hi is None:
        return False
    return lo > hi or (lo == hi and not (lo_incl and hi_incl))


def _is_point(iv: _Interval) -> bool:
    _kind, lo, lo_incl, hi, hi_incl = iv
    return lo is not None and lo == hi and lo_incl and hi_incl


def _interval_entails(atoms: list[tuple[str, _Lit]], op: str, lit: _Lit) -> bool:
    iv = _interval(atoms)
    if iv is None:
        return False
    if _is_empty(iv):
        return True  # an unsatisfiable strong implies anything (vacuously)
    kind, lo, lo_incl, hi, hi_incl = iv
    wkind, wv = lit
    if kind != wkind:
        return False
    # A non-strict weak bound (>=, <=) needs only that the strong bound reaches it;
    # the strong bound's own strictness does not matter. A strict weak bound (>, <)
    # additionally rules out equality unless the strong bound already excludes it.
    if op == ">=":
        return lo is not None and lo >= wv
    if op == ">":
        return lo is not None and (lo > wv or (lo == wv and not lo_incl))
    if op == "<=":
        return hi is not None and hi <= wv
    if op == "<":
        return hi is not None and (hi < wv or (hi == wv and not hi_incl))
    return lo is not None and lo == hi == wv and lo_incl and hi_incl  # "="


def _set_entails(in_set: frozenset[_Lit], op: str, lit: _Lit) -> bool:
    wkind, wv = lit
    return bool(in_set) and all(kind == wkind and _cmp(v, op, wv) for kind, v in in_set)


def _cmp(x: Any, op: str, y: Any) -> bool:
    return {
        "<": x < y,
        "<=": x <= y,
        ">": x > y,
        ">=": x >= y,
        "=": x == y,
    }[op]
