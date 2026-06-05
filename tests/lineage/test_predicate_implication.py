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

from hypothesis import given
from hypothesis import strategies as st
from sqlglot import Expr

from dblect.lineage.predicate import implies, parse_predicate

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


# --- soundness PBT: a True verdict is never wrong --------------------------------

_COLS = ("a", "b", "c")
_OPS = ("<", "<=", ">", ">=", "=")


@st.composite
def _atom(draw: st.DrawFn) -> tuple[str, str, int]:
    return (draw(st.sampled_from(_COLS)), draw(st.sampled_from(_OPS)), draw(st.integers(-4, 4)))


def _atom_sql(atom: tuple[str, str, int]) -> str:
    col, op, lit = atom
    return f"{col} {op} {lit}"


def _conj_sql(atoms: list[tuple[str, str, int]]) -> str:
    return " AND ".join(_atom_sql(a) for a in atoms)


def _holds(atom: tuple[str, str, int], world: dict[str, int]) -> bool:
    _col, op, lit = atom
    v = world[atom[0]]
    return {
        "<": v < lit,
        "<=": v <= lit,
        ">": v > lit,
        ">=": v >= lit,
        "=": v == lit,
    }[op]


@given(
    strong=st.lists(_atom(), min_size=1, max_size=4),
    weak=st.lists(_atom(), min_size=1, max_size=3),
)
def test_implies_is_sound_over_integer_worlds(
    strong: list[tuple[str, str, int]], weak: list[tuple[str, str, int]]
) -> None:
    """If the engine certifies ``strong ⟹ weak``, then every integer assignment
    satisfying ``strong`` must satisfy ``weak``. The check samples the small
    integer cube exhaustively, so an unsound certification cannot hide."""
    verdict = _implies(_conj_sql(strong), _conj_sql(weak))
    if not verdict:
        return  # incompleteness is allowed; only a True verdict carries an obligation
    cube = range(-6, 7)
    for av in cube:
        for bv in cube:
            for cv in cube:
                world = {"a": av, "b": bv, "c": cv}
                if all(_holds(a, world) for a in strong):
                    assert all(_holds(w, world) for w in weak), (
                        f"unsound: {_conj_sql(strong)!r} ⟹ {_conj_sql(weak)!r} "
                        f"certified but fails at {world}"
                    )
