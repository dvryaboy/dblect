"""Soundness PBT for the grain-established decision procedure.

``grain_established`` claims an entailment: a relation unique on each inferred key
and satisfying the given functional dependencies is unique on the declared grain.
The property checks that claim against brute force. Generate a small relation, hand
the decision only claims the relation actually satisfies (its true uniqueness sets
and true dependencies, any subset of them), and require that whenever the decision
answers "established" the relation is in fact unique on the declared columns. The
decision may be conservative (a False on a relation that happens to be unique is
fine); it must never be credulous.
"""

from __future__ import annotations

from itertools import combinations

from hypothesis import given
from hypothesis import strategies as st

from dblect.check.grain import grain_established, grain_witness
from dblect.lineage.properties.functional_dependency import FD, FDSet

_COLS = ("c0", "c1", "c2", "c3")

_ROWS = st.lists(
    st.tuples(*(st.integers(0, 2) for _ in _COLS)),
    min_size=0,
    max_size=6,
)

_INDEX_SUBSETS: tuple[tuple[int, ...], ...] = tuple(
    subset for n in range(1, len(_COLS) + 1) for subset in combinations(range(len(_COLS)), n)
)


def _unique_on(relation: list[tuple[int, ...]], cols: tuple[int, ...]) -> bool:
    projected = [tuple(row[i] for i in cols) for row in relation]
    return len(projected) == len(set(projected))


def _satisfies_fd(relation: list[tuple[int, ...]], det: tuple[int, ...], target: int) -> bool:
    seen: dict[tuple[int, ...], int] = {}
    for row in relation:
        key = tuple(row[i] for i in det)
        if key in seen and seen[key] != row[target]:
            return False
        seen.setdefault(key, row[target])
    return True


def _names(indices: tuple[int, ...]) -> frozenset[str]:
    return frozenset(_COLS[i] for i in indices)


@given(_ROWS, st.data())
def test_established_implies_uniqueness_on_the_declared_grain(
    relation: list[tuple[int, ...]], data: st.DataObject
) -> None:
    true_keys = [s for s in _INDEX_SUBSETS if _unique_on(relation, s)]
    true_fds = [
        (det, target)
        for det in _INDEX_SUBSETS
        for target in range(len(_COLS))
        if target not in det and _satisfies_fd(relation, det, target)
    ]
    claimed_keys = (
        data.draw(st.lists(st.sampled_from(true_keys), max_size=4), label="keys")
        if true_keys
        else []
    )
    claimed_fds = (
        data.draw(st.lists(st.sampled_from(true_fds), max_size=4), label="fds") if true_fds else []
    )
    grain = data.draw(st.sampled_from(_INDEX_SUBSETS), label="grain")

    inferred = frozenset(_names(k) for k in claimed_keys)
    fds = FDSet.of(*(FD(_names(det), _COLS[target]) for det, target in claimed_fds))

    if grain_established(_names(grain), inferred, fds):
        assert _unique_on(relation, grain)


# --- the edges the closure marks, pinned as counterexamples -------------------------


def test_fd_closure_is_what_licenses_coverage() -> None:
    # unique on (a, b) plus a -> b entails unique on (a); without the dependency the
    # same key entails nothing about (a) (two rows (1, 1) and (1, 2) separate them).
    grain = frozenset({"a"})
    inferred = frozenset({frozenset({"a", "b"})})
    assert grain_established(grain, inferred, FDSet.of(FD(frozenset({"a"}), "b")))
    assert not grain_established(grain, inferred, FDSet.of())


def test_no_inferred_key_never_establishes() -> None:
    # FDs alone say nothing about row multiplicity: a -> b holds on a relation with
    # duplicate (a, b) rows, so with no uniqueness claim at all nothing establishes.
    grain = frozenset({"a"})
    assert not grain_established(grain, frozenset(), FDSet.of(FD(frozenset({"a"}), "b")))


def test_witness_requires_a_strictly_finer_key() -> None:
    # (a, b) defeats a declared grain (a); a disjoint key (c) says nothing about
    # rows per (a) and must not witness. The witness is the smallest finer key.
    fine = frozenset({"a", "b"})
    assert grain_witness(frozenset({"a"}), frozenset({fine})) == fine
    assert grain_witness(frozenset({"a"}), frozenset({frozenset({"c"})})) is None
    assert grain_witness(frozenset({"a"}), frozenset({frozenset({"a"})})) is None
