"""Checking the coverage decision against brute force over small tables.

``covers`` answers "does this determinant functionally determine every column of
some key, under these dependencies?" Here we build an actual small table, work out
which keys and dependencies genuinely hold over its rows, hand the decision some of
those true facts, and require that any time it answers yes, the table really is
unique on the determinant.

Only that direction is a bug. Answering no about a table that happens to be unique
on the determinant costs a missed finding; answering yes about one that is not would
let a wrong claim through, which is what these rule out.
"""

from __future__ import annotations

from itertools import combinations

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.properties.functional_dependency import FD, FDSet, covers

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
def test_covered_implies_uniqueness_on_the_determinant(
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
    determinant = data.draw(st.sampled_from(_INDEX_SUBSETS), label="determinant")

    keys = frozenset(_names(k) for k in claimed_keys)
    fds = FDSet.of(*(FD(_names(det), _COLS[target]) for det, target in claimed_fds))

    if covers(fds, _names(determinant), keys):
        assert _unique_on(relation, determinant)


# --- the edges the closure marks, pinned as counterexamples -------------------------


def test_coverage_is_containment_through_the_closure_not_an_exact_match() -> None:
    # A key within the given columns is covered: unique on (a) is unique on
    # (a, b), so a non-minimal given set is covered.
    assert covers(FDSet.of(), frozenset({"a", "b"}), frozenset({frozenset({"a"})}))
    # The closure extends that reach: unique on (a, b) plus a -> b entails unique on
    # (a), where without the dependency the same key entails nothing about (a) (two
    # rows (1, 1) and (1, 2) separate them).
    given = frozenset({"a"})
    keys = frozenset({frozenset({"a", "b"})})
    assert covers(FDSet.of(FD(frozenset({"a"}), "b")), given, keys)
    assert not covers(FDSet.of(), given, keys)


def test_no_keys_never_covers() -> None:
    # FDs alone say nothing about row multiplicity: a -> b holds on a relation with
    # duplicate (a, b) rows, so with no key claim at all nothing is covered.
    given = frozenset({"a"})
    assert not covers(FDSet.of(FD(frozenset({"a"}), "b")), given, frozenset())
