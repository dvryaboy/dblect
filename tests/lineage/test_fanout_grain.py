"""Fan-out grain signal at a sum (the C3 join concern, substrate level).

A magnitude is summable only when the rows being folded are distinct at the grain that
produced it: each underlying value counted once. A join to a many-side replicates the
magnitude (the fan trap), so a downstream ``sum`` double counts. The substrate reads this
from the uniqueness property: :func:`grain_preserved` asks whether the relation being
aggregated is still keyed at least as finely as the magnitude's origin key, so each origin
row appears once.

This is the signal only. The user-facing ``order_revenue``-style finding (fired where the
grain is *not* preserved through a join) belongs to the finding pipeline, a later build;
here the predicate is pinned directly. Uniqueness's own join handling, which drops a key a
fan-out breaks, is tested in the uniqueness suite, so this pins the grain predicate over
constructed key sets.
"""

from __future__ import annotations

from dblect.lineage.properties.uniqueness import NO_KEYS, CandidateKeySet, grain_preserved


def _keys(*key_sets: set[str]) -> CandidateKeySet:
    return CandidateKeySet(frozenset(frozenset(k) for k in key_sets))


def test_grain_preserved_when_the_origin_key_is_a_key() -> None:
    assert grain_preserved(_keys({"order_id"}), frozenset({"order_id"}))


def test_grain_preserved_when_a_finer_key_survives() -> None:
    """A relation keyed on a subset of the origin is even finer, so each origin row still
    appears once."""
    assert grain_preserved(_keys({"order_id"}), frozenset({"order_id", "line_no"}))


def test_grain_not_preserved_when_only_a_different_key_survives() -> None:
    """The fan trap: joining orders to line items leaves the result keyed on the line
    grain, not the order grain, so ``sum(order_total)`` would double count."""
    assert not grain_preserved(_keys({"line_item_id"}), frozenset({"order_id"}))


def test_grain_not_preserved_when_no_key_is_known() -> None:
    """No surviving key proves the grain, so the sum is not provably single-counted."""
    assert not grain_preserved(NO_KEYS, frozenset({"order_id"}))


def test_any_surviving_subset_key_preserves_the_grain() -> None:
    """Several candidate keys: one that refines the origin is enough."""
    assert grain_preserved(_keys({"x"}, {"order_id"}), frozenset({"order_id"}))
