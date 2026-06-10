"""The FD-set lattice for the functional-dependency property, and its entailment.

Functional dependencies order by precision where knowing more dependencies is more
precise, exactly as uniqueness orders keys: ``meet`` unions the FD sets (two
declarations both hold), ``join`` intersects them (a confluence keeps only shared
dependencies), ``top`` is the empty set, and ``bottom`` is a formal universal
element no real resolution reaches. The shared :func:`assert_lattice_laws` pins
the bounded-lattice algebra.

``determines`` is pinned against the semantic definition of FD entailment rather
than a restated closure: a set of FDs entails ``X -> t`` exactly when every
two-row relation satisfying the set satisfies ``X -> t`` (the two-row construction
in the classic completeness proof for Armstrong's axioms), so the test enumerates
those models and demands agreement. That keeps the test honest about both
directions: an under-claiming ``determines`` misses an entailed dependency, an
over-claiming one breaks soundness.
"""

from __future__ import annotations

from itertools import combinations, product

from hypothesis import given, settings
from hypothesis import strategies as st

from dblect.lineage.facts.lattice import resolve
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.functional_dependency import (
    ALL_FDS,
    FD,
    FUNCTIONAL_DEPENDENCY_LATTICE,
    NO_FDS,
    FDSet,
    determines,
)
from tests.lineage._lattice_laws import assert_consistency_laws, assert_lattice_laws

_COLS = ("a", "b", "c", "d")

_col_sets = st.frozensets(st.sampled_from(_COLS), max_size=3)
_fds = st.builds(FD, determinant=_col_sets, dependent=st.sampled_from(_COLS))
_fd_sets = st.frozensets(_fds, max_size=4).map(FDSet)
# Mostly real FD sets, occasionally the bottom sentinel, so the law arms that
# touch bottom are exercised without swamping the normal cases.
_values = st.one_of(_fd_sets, st.just(ALL_FDS))

_REL = SourceRef(SourceKind.MODEL, "model.shop.payments")


def _fact(value: FDSet) -> Fact[FDSet, SourceRef]:
    return Fact(scope=_REL, value=value, provenance=Declared(DeclaredSource.USER_ASSERTED))


@given(_values, _values, _values)
def test_fd_lattice_laws(a: FDSet, b: FDSet, c: FDSet) -> None:
    assert_lattice_laws(FUNCTIONAL_DEPENDENCY_LATTICE, a, b, c)


@given(_values, _values)
def test_fd_consistency_laws(declared: FDSet, value: FDSet) -> None:
    assert_consistency_laws(FUNCTIONAL_DEPENDENCY_LATTICE, declared, value)


def test_top_is_no_fds() -> None:
    assert FUNCTIONAL_DEPENDENCY_LATTICE.top == NO_FDS
    assert NO_FDS.fds == frozenset()
    assert not NO_FDS.is_bottom


def test_meet_unions_fds() -> None:
    """Resolving two declared dependencies keeps both."""
    a = FDSet.of(FD(frozenset({"country"}), "currency"))
    b = FDSet.of(FD(frozenset({"country"}), "region"))
    assert FUNCTIONAL_DEPENDENCY_LATTICE.meet(a, b) == FDSet(a.fds | b.fds)


def test_join_intersects_fds() -> None:
    """A confluence keeps only the dependencies both branches carry."""
    shared = FD(frozenset({"country"}), "currency")
    a = FDSet.of(shared, FD(frozenset({"country"}), "region"))
    b = FDSet.of(shared)
    assert FUNCTIONAL_DEPENDENCY_LATTICE.join(a, b) == FDSet.of(shared)


@given(st.lists(_fd_sets, max_size=6))
def test_resolution_never_contradicts(values: list[FDSet]) -> None:
    """FD declarations only ever union, so a bucket of real FD sets resolves to
    their combined dependencies and never reports a contradiction."""
    facts = tuple(_fact(v) for v in values)
    value, is_contradiction = resolve(FUNCTIONAL_DEPENDENCY_LATTICE, facts)
    assert not is_contradiction
    expected: frozenset[FD] = frozenset()
    for v in values:
        expected = expected | v.fds
    assert value == FDSet(expected)


# --- entailment ----------------------------------------------------------------


def _holds(rows: tuple[dict[str, int], ...], determinant: frozenset[str], dependent: str) -> bool:
    return all(
        r1[dependent] == r2[dependent]
        for r1, r2 in combinations(rows, 2)
        if all(r1[col] == r2[col] for col in determinant)
    )


def _semantically_entails(sigma: FDSet, given: frozenset[str], target: str) -> bool:
    """Whether every two-row {0,1} relation satisfying ``sigma`` satisfies
    ``given -> target``. Two-row models decide FD entailment, and only the per-column
    agreement pattern matters, so a binary domain is exhaustive."""
    all_rows: list[dict[str, int]] = [
        dict(zip(_COLS, bits, strict=True)) for bits in product((0, 1), repeat=len(_COLS))
    ]
    for pair in combinations(all_rows, 2):
        if all(_holds(pair, fd.determinant, fd.dependent) for fd in sigma.fds) and not _holds(
            pair, given, target
        ):
            return False
    return True


@given(_fd_sets, _col_sets, st.sampled_from(_COLS))
@settings(max_examples=1000)
def test_determines_matches_two_row_semantics(
    sigma: FDSet, given: frozenset[str], target: str
) -> None:
    """Order-sensitive closure bugs (a single non-fixpoint pass over the FD set) hide
    in the small fraction of draws whose entailment needs a chained derivation, so
    this runs deep enough to reach them reliably."""
    assert determines(sigma, given, target) == _semantically_entails(sigma, given, target)


def test_bottom_entails_everything() -> None:
    assert determines(ALL_FDS, frozenset(), "currency")


def test_empty_determinant_means_constant() -> None:
    """``{} -> currency`` says the column is constant over the whole relation, so any
    group key discharges it."""
    constant = FDSet.of(FD(frozenset(), "currency"))
    assert determines(constant, frozenset(), "currency")
    assert determines(constant, frozenset({"country"}), "currency")


def test_transitivity_chains_dependencies() -> None:
    chain = FDSet.of(FD(frozenset({"a"}), "b"), FD(frozenset({"b"}), "c"))
    assert determines(chain, frozenset({"a"}), "c")
    assert not determines(chain, frozenset({"c"}), "a")
