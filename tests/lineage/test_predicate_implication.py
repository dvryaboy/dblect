"""The predicate implication engine: a sound, conservative ``strong ⟹ weak``.

Activation of a conditional fact asks one question: does the row filter that
reached a scope imply the predicate the fact is scoped to? If yes, the scope's
rows are a subset of the fact's rows, so the claim (a key, a NOT_NULL) carries.
``implies(strong, weak)`` answers it, returning ``True`` only when it can *prove*
``strong ⟹ weak``; an unrecognised shape returns ``False`` (we stay silent rather
than over-claim). These pin the proof rules and, via PBT, the one property that
must never break: a ``True`` verdict is never wrong.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Generic, TypeVar

from hypothesis import given
from hypothesis import strategies as st
from sqlglot import Expr

from dblect.lineage.predicate import (
    Canon,
    atoms_of,
    entailment_checker,
    entails_atoms,
    implies,
    parse_predicate,
)

_DIALECT = "duckdb"


def _p(sql: str) -> Expr:
    parsed = parse_predicate(sql, dialect=_DIALECT)
    assert parsed is not None, f"could not parse predicate: {sql!r}"
    return parsed


def _implies(strong: str, weak: str) -> bool:
    return implies(_p(strong), _p(weak))


# --- identity, canonicalisation --------------------------------------------------


def test_identical_predicate_implies_itself() -> None:
    assert _implies("a >= 5", "a >= 5")


def test_comparison_operands_canonicalise() -> None:
    # `5 <= a` is `a >= 5`; the engine compares the canonical form.
    assert _implies("5 <= a", "a >= 5")


def test_redundant_parens_and_case_do_not_matter() -> None:
    assert _implies("(A >= 5)", "a >= 5")


# --- interval narrowing (the date-filter case, in miniature) ---------------------


def test_narrower_lower_bound_implies_wider() -> None:
    assert _implies("a >= 10", "a >= 5")


def test_wider_lower_bound_does_not_imply_narrower() -> None:
    assert not _implies("a >= 5", "a >= 10")


def test_equality_implies_a_containing_range() -> None:
    assert _implies("a = 7", "a >= 5")
    assert not _implies("a = 3", "a >= 5")


def test_strict_and_nonstrict_bounds() -> None:
    assert _implies("a > 5", "a >= 5")
    assert not _implies("a >= 5", "a > 5")


def test_date_literals_order_lexically() -> None:
    assert _implies("d >= '2024-01-01'", "d >= '2020-01-01'")
    assert not _implies("d >= '2020-01-01'", "d >= '2024-01-01'")


# --- conjunction subsumption (consumer adds extra filters) -----------------------


def test_extra_conjunct_still_implies_each_conjunct() -> None:
    assert _implies("a >= 5 AND b = 3", "a >= 5")
    assert _implies("country = 'US' AND active", "country = 'US'")


def test_fewer_conjuncts_do_not_imply_more() -> None:
    assert not _implies("a >= 5", "a >= 5 AND b = 3")


def test_conjunctive_weak_needs_every_conjunct() -> None:
    assert _implies("a >= 5 AND b = 3", "a >= 5 AND b = 3")
    assert _implies("a >= 10 AND b = 3 AND c = 1", "a >= 5 AND b = 3")
    assert not _implies("a >= 10 AND b = 3", "a >= 5 AND b = 9")


def test_unmodelable_conjunct_is_ignored_not_fatal() -> None:
    # `lower(x) = 'y'` is not in the fragment; dropping it leaves a weaker strong,
    # which still proves the bound. Dropping a conjunct is the safe direction.
    assert _implies("a >= 10 AND lower(x) = 'y'", "a >= 5")


# --- IN membership ---------------------------------------------------------------


def test_in_subset_implies_superset() -> None:
    assert _implies("a IN (1, 2)", "a IN (1, 2, 3)")
    assert not _implies("a IN (1, 2, 3)", "a IN (1, 2)")


def test_in_implies_a_covering_range() -> None:
    assert _implies("a IN (5, 7, 9)", "a >= 5")
    assert not _implies("a IN (1, 7)", "a >= 5")


# --- monotonic truncation (date/time bucketing) ----------------------------------


def test_date_trunc_same_term_narrows() -> None:
    assert _implies("date_trunc('day', d) >= '2024-01-01'", "date_trunc('day', d) >= '2020-01-01'")
    assert not _implies(
        "date_trunc('day', d) >= '2020-01-01'", "date_trunc('day', d) >= '2024-01-01'"
    )


def test_date_trunc_distinguishes_unit_and_column() -> None:
    # Different unit or different column is a different term: no implication.
    assert not _implies(
        "date_trunc('day', d) >= '2024-01-01'", "date_trunc('month', d) >= '2020-01-01'"
    )
    assert not _implies(
        "date_trunc('day', d1) >= '2024-01-01'", "date_trunc('day', d2) >= '2020-01-01'"
    )


# --- conservative on shapes outside the fragment ---------------------------------


def test_different_column_never_implies() -> None:
    assert not _implies("a >= 5", "b >= 5")


def test_arithmetic_term_is_not_reasoned_about() -> None:
    assert not _implies("a + 1 >= 6", "a >= 5")


def test_disjunction_in_weak_is_sufficient_when_one_arm_holds() -> None:
    assert _implies("a >= 10", "a >= 5 OR b >= 100")


def test_disjunction_in_strong_needs_every_arm() -> None:
    assert _implies("a >= 10 OR a >= 20", "a >= 5")
    assert not _implies("a >= 10 OR b >= 10", "a >= 5")


def test_unparseable_or_empty_predicate_is_no_information() -> None:
    assert parse_predicate("???", dialect=_DIALECT) is None


# --- entails_atoms: the atom-set form, for activation ----------------------------


def _strong(sql: str) -> frozenset[Canon]:
    parsed = parse_predicate(sql, dialect=_DIALECT)
    assert parsed is not None
    return atoms_of(parsed)


def test_entails_atoms_proves_a_weaker_bound_from_collected_atoms() -> None:
    assert entails_atoms(_strong("a >= 10 AND b = 3"), _strong("a >= 5"))
    assert entails_atoms(_strong("a >= 10 AND b = 3"), _strong("a >= 5 AND b = 3"))
    assert not entails_atoms(_strong("a >= 5"), _strong("a >= 10"))


def test_entails_atoms_matches_a_conjunct_syntactically() -> None:
    assert entails_atoms(_strong("country = 'US' AND active"), _strong("country = 'US'"))
    # A bare boolean is opaque; only an exact-atom match proves it.
    assert entails_atoms(_strong("active"), _strong("active"))
    assert not entails_atoms(_strong("active"), _strong("inactive"))


def test_entails_atoms_is_in_subset_aware() -> None:
    assert entails_atoms(_strong("a IN (1, 2)"), _strong("a IN (1, 2, 3)"))
    assert not entails_atoms(_strong("a IN (1, 2, 3)"), _strong("a IN (1, 2)"))


def test_entailment_checker_reuses_one_fold_across_weak_sets() -> None:
    # A disjunctive WHERE flattens to a single opaque atom (no AND to split), so it
    # entails only an identical atom, not its weaker arms. This is the conservative
    # boundary the disjunction follow-up (#61) lifts; pin it so the gap is intentional.
    check = entailment_checker(_strong("a >= 10"))
    assert check(_strong("a >= 5"))
    assert not check(_strong("a >= 5 OR z >= 100"))


# --- soundness PBT: a True verdict is never wrong --------------------------------
#
# A generated predicate is a conjunction of clauses; each clause is a single atom or
# a two-atom OR. An atom is ``term <op> literal`` or ``term IN (literals)``. We render
# it to SQL, feed both sides to the engine, and only when it certifies ``strong ⟹
# weak`` do we discharge the obligation: over every world (an independent value per
# term), no world satisfying ``strong`` may violate ``weak``. A constrained TypeVar
# keeps the same generators sound for both the numeric and the lexical domains.

_OPS = ("<", "<=", ">", ">=", "=")
_V = TypeVar("_V", int, str)  # a literal: a number, or a string ordered lexically


@dataclass(frozen=True)
class _TermSpec:
    """A term's SQL text paired with the world key it reads. ``date_trunc('day', a)``
    keys a *different* world variable than ``a``: the engine treats the truncation as
    its own opaque term and never relates it back to the column, so assigning the two
    independently models a superset of real ``(a, trunc(a))`` worlds. Soundness over
    that superset is the stronger claim."""

    sql: str
    key: str


@dataclass(frozen=True)
class _Cmp(Generic[_V]):
    term: _TermSpec
    op: str
    lit: _V


@dataclass(frozen=True)
class _In(Generic[_V]):
    term: _TermSpec
    members: tuple[_V, ...]


@dataclass(frozen=True)
class _Or(Generic[_V]):
    left: _Cmp[_V] | _In[_V]
    right: _Cmp[_V] | _In[_V]


_Clause = _Cmp[_V] | _In[_V] | _Or[_V]


def _sql_lit(v: int | str) -> str:
    return f"'{v}'" if isinstance(v, str) else str(v)


def _render_leaf(leaf: _Cmp[_V] | _In[_V]) -> str:
    if isinstance(leaf, _Cmp):
        return f"{leaf.term.sql} {leaf.op} {_sql_lit(leaf.lit)}"
    return f"{leaf.term.sql} IN ({', '.join(_sql_lit(m) for m in leaf.members)})"


def _render(clauses: list[_Clause[_V]]) -> str:
    parts = [
        f"({_render_leaf(c.left)} OR {_render_leaf(c.right)})"
        if isinstance(c, _Or)
        else _render_leaf(c)
        for c in clauses
    ]
    return " AND ".join(parts)


def _cmp(v: _V, op: str, lit: _V) -> bool:
    return {"<": v < lit, "<=": v <= lit, ">": v > lit, ">=": v >= lit, "=": v == lit}[op]


def _holds_leaf(leaf: _Cmp[_V] | _In[_V], world: dict[str, _V]) -> bool:
    if isinstance(leaf, _Cmp):
        return _cmp(world[leaf.term.key], leaf.op, leaf.lit)
    return world[leaf.term.key] in leaf.members


def _holds(clauses: list[_Clause[_V]], world: dict[str, _V]) -> bool:
    return all(
        (_holds_leaf(c.left, world) or _holds_leaf(c.right, world))
        if isinstance(c, _Or)
        else _holds_leaf(c, world)
        for c in clauses
    )


def _keys(clauses: list[_Clause[_V]]) -> set[str]:
    ks: set[str] = set()
    for c in clauses:
        leaves = (c.left, c.right) if isinstance(c, _Or) else (c,)
        ks.update(leaf.term.key for leaf in leaves)
    return ks


def _assert_sound(
    strong: list[_Clause[_V]], weak: list[_Clause[_V]], domain: tuple[_V, ...]
) -> None:
    if not implies(_p(_render(strong)), _p(_render(weak))):
        return  # incompleteness is allowed; only a True verdict carries an obligation
    keys = sorted(_keys(strong) | _keys(weak))
    for combo in itertools.product(domain, repeat=len(keys)):
        world = dict(zip(keys, combo, strict=True))
        if _holds(strong, world) and not _holds(weak, world):
            raise AssertionError(
                f"unsound: {_render(strong)!r} ⟹ {_render(weak)!r} certified but fails at {world}"
            )


def _leaves(leaf_st: st.SearchStrategy[_Cmp[_V] | _In[_V]]) -> st.SearchStrategy[_Clause[_V]]:
    return st.one_of(leaf_st, st.builds(_Or, leaf_st, leaf_st))


# Numeric fragment: columns, a monotonic truncation term, comparisons, IN, OR.
_NUM_TERMS = (_TermSpec("a", "a"), _TermSpec("b", "b"), _TermSpec("date_trunc('day', a)", "td"))
_INT = st.integers(-4, 4)
_NUM_LEAF: st.SearchStrategy[_Cmp[int] | _In[int]] = st.one_of(
    st.builds(_Cmp, st.sampled_from(_NUM_TERMS), st.sampled_from(_OPS), _INT),
    st.builds(
        _In,
        st.sampled_from(_NUM_TERMS),
        st.lists(_INT, min_size=1, max_size=3, unique=True).map(tuple),
    ),
)
_NUM_CLAUSE = _leaves(_NUM_LEAF)


@given(
    strong=st.lists(_NUM_CLAUSE, min_size=1, max_size=3),
    weak=st.lists(_NUM_CLAUSE, min_size=1, max_size=2),
)
def test_implies_is_sound_over_term_worlds(
    strong: list[_Clause[int]], weak: list[_Clause[int]]
) -> None:
    """A certified ``strong ⟹ weak`` holds for every independent integer assignment
    of the terms. Covers comparisons, ``IN``, ``OR``, and truncation terms; the cube
    is sampled exhaustively so an unsound certification cannot hide."""
    _assert_sound(strong, weak, tuple(range(-6, 7)))


# Lexical fragment: one string column, so the literals exercise string ordering.
_STR_COL = (_TermSpec("s", "s"),)
_STR_LIT = st.sampled_from(("b", "d", "f", "h"))
_STR_LEAF: st.SearchStrategy[_Cmp[str] | _In[str]] = st.one_of(
    st.builds(_Cmp, st.sampled_from(_STR_COL), st.sampled_from(_OPS), _STR_LIT),
    st.builds(
        _In,
        st.sampled_from(_STR_COL),
        st.lists(_STR_LIT, min_size=1, max_size=3, unique=True).map(tuple),
    ),
)
_STR_CLAUSE = _leaves(_STR_LEAF)
# Worlds straddle the literals, including a between-value and a lexical edge case
# (``"bb"`` sorts after ``"b"`` but before ``"c"``).
_STR_WORLD = ("a", "b", "bb", "c", "e", "g", "i")


@given(
    strong=st.lists(_STR_CLAUSE, min_size=1, max_size=3),
    weak=st.lists(_STR_CLAUSE, min_size=1, max_size=2),
)
def test_implies_is_sound_over_string_worlds(
    strong: list[_Clause[str]], weak: list[_Clause[str]]
) -> None:
    """A certified ``strong ⟹ weak`` over string literals holds for every lexically
    ordered world. This is the date-bound shape (ISO strings order lexically)."""
    _assert_sound(strong, weak, _STR_WORLD)
