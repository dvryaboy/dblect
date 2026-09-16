"""``grain_witness`` picks the evidence a grain-not-established finding cites: the
smallest derived key that strictly refines past the declared grain.
"""

from __future__ import annotations

from dblect.check.grain import grain_witness


def test_witness_requires_a_strictly_finer_key() -> None:
    # (a, b) defeats a declared grain (a); a disjoint key (c) says nothing about
    # rows per (a) and must not witness. The witness is the smallest finer key.
    fine = frozenset({"a", "b"})
    assert grain_witness(frozenset({"a"}), frozenset({fine})) == fine
    assert grain_witness(frozenset({"a"}), frozenset({frozenset({"c"})})) is None
    assert grain_witness(frozenset({"a"}), frozenset({frozenset({"a"})})) is None
