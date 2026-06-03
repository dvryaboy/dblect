"""The seam combine, fact collection, and grounding.

Written against the contract in ``lineage-facts.md``: combine clears a concrete
value at a typed/untyped seam and speaks or stays silent by the cleared
operand's opacity; collect buckets facts by scope and isolates a failing
discoverer; grounding turns a scope's bucket into its grounded annotation
(EXPLICIT opt-out, CONCRETE value, or IMPLICIT default).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.facts.grounding import (
    DiscovererError,
    FactConflictError,
    SeamContradictionError,
    collect,
    combine,
    grounding,
)
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Declared, DeclaredSource, Fact, Opacity
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.manifest import Manifest

# A flat lattice over a tiny domain: two committed values "A"/"B" that meet to
# bottom, plus an explicit top and bottom sentinel.
_TOP = "TOP"
_BOTTOM = "BOTTOM"


def _flat_meet(a: str, b: str) -> str:
    if a == b:
        return a
    if a == _TOP:
        return b
    if b == _TOP:
        return a
    return _BOTTOM  # two distinct committed values, or anything with bottom


def _flat_join(a: str, b: str) -> str:
    if a == b:
        return a
    if a == _BOTTOM:
        return b
    if b == _BOTTOM:
        return a
    return _TOP


_FLAT: Lattice[str] = Lattice(meet=_flat_meet, join=_flat_join, top=_TOP, bottom=_BOTTOM)

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.orders")
_COL_A = ColumnRef(_SRC, "a")
_COL_B = ColumnRef(_SRC, "b")
_EMPTY_MANIFEST = Manifest(schema_version="1", adapter_type="duckdb", nodes={})


def _fact(scope: ColumnRef, value: str) -> Fact[str, ColumnRef]:
    return Fact(scope=scope, value=value, provenance=Declared(DeclaredSource.DBT_GENERIC_TEST))


# --- combine (the typed/untyped seam) ---------------------------------------


def test_combine_preserves_agreeing_concrete_values() -> None:
    out = combine(_FLAT, Annotation("A"), Annotation("A"))
    assert out == Annotation("A", Opacity.CONCRETE, provisional=False)


def test_combine_implicit_top_clears_and_speaks() -> None:
    """A committed value meeting an un-annotated (IMPLICIT) top clears to top and
    inherits IMPLICIT, the opacity the seam diagnostic warns on."""
    out = combine(_FLAT, Annotation("A"), Annotation(_TOP, Opacity.IMPLICIT))
    assert out.value == _TOP
    assert out.opacity is Opacity.IMPLICIT


def test_combine_explicit_top_clears_silently() -> None:
    """A declared opt-out (EXPLICIT) top clears to top but flows silently."""
    out = combine(_FLAT, Annotation("A"), Annotation(_TOP, Opacity.EXPLICIT))
    assert out.value == _TOP
    assert out.opacity is Opacity.EXPLICIT


def test_combine_two_committed_incompatible_raises() -> None:
    with pytest.raises(SeamContradictionError):
        combine(_FLAT, Annotation("A"), Annotation("B"))


def test_combine_explicit_dominates_implicit_at_top() -> None:
    """Two tops agree on the value; a declared opt-out is not downgraded to
    incidental."""
    out = combine(_FLAT, Annotation(_TOP, Opacity.EXPLICIT), Annotation(_TOP, Opacity.IMPLICIT))
    assert out.value == _TOP
    assert out.opacity is Opacity.EXPLICIT


def test_combine_propagates_provisional() -> None:
    out = combine(_FLAT, Annotation("A", provisional=True), Annotation("A"))
    assert out.provisional


@st.composite
def _well_formed_annotation(draw: st.DrawFn) -> Annotation[str]:
    """A committed value carries CONCRETE; a top carries IMPLICIT or EXPLICIT. The
    substrate maintains this invariant (a top is never CONCRETE), and combine's
    opacity choice on an agreeing top relies on it."""
    value = draw(st.sampled_from([_TOP, "A", "B"]))
    provisional = draw(st.booleans())
    if value == _TOP:
        opacity = draw(st.sampled_from([Opacity.IMPLICIT, Opacity.EXPLICIT]))
    else:
        opacity = Opacity.CONCRETE
    return Annotation(value, opacity, provisional=provisional)


@given(_well_formed_annotation(), _well_formed_annotation())
def test_combine_is_commutative(a: Annotation[str], b: Annotation[str]) -> None:
    """combine is symmetric: value, opacity, and taint do not depend on operand
    order, and a contradiction is raised for both orders or neither."""
    try:
        forward = combine(_FLAT, a, b)
    except SeamContradictionError:
        with pytest.raises(SeamContradictionError):
            combine(_FLAT, b, a)
        return
    assert combine(_FLAT, b, a) == forward


@given(_well_formed_annotation(), _well_formed_annotation())
def test_combine_value_and_taint_invariants(a: Annotation[str], b: Annotation[str]) -> None:
    """Off the contradiction arm, the result is the meet or a cleared top, never
    bottom, and the provisional taint is the OR of the operands'."""
    try:
        out = combine(_FLAT, a, b)
    except SeamContradictionError:
        return
    assert out.value in (_FLAT.meet(a.value, b.value), _FLAT.top)
    assert out.value != _FLAT.bottom
    assert out.provisional == (a.provisional or b.provisional)


# --- collect -----------------------------------------------------------------


class _StaticDiscoverer:
    def __init__(self, facts: tuple[Fact[str, ColumnRef], ...]) -> None:
        self._facts = facts

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[str, ColumnRef]]:
        return self._facts


class _RaisingDiscoverer:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[str, ColumnRef]]:
        raise self._exc


def test_collect_buckets_facts_by_scope() -> None:
    d1 = _StaticDiscoverer((_fact(_COL_A, "A"),))
    d2 = _StaticDiscoverer((_fact(_COL_A, "A"), _fact(_COL_B, "B")))
    buckets = collect(_EMPTY_MANIFEST, (d1, d2), name_to_source={})
    assert set(buckets) == {_COL_A, _COL_B}
    assert len(buckets[_COL_A]) == 2
    assert len(buckets[_COL_B]) == 1


def test_collect_isolates_a_failing_discoverer() -> None:
    """A DiscovererError drops only that discoverer's facts; the rest survive."""
    good = _StaticDiscoverer((_fact(_COL_A, "A"),))
    bad = _RaisingDiscoverer(DiscovererError("manifest shape surprised me"))
    buckets = collect(_EMPTY_MANIFEST, (bad, good), name_to_source={})
    assert set(buckets) == {_COL_A}


def test_collect_propagates_unexpected_errors() -> None:
    """Anything other than DiscovererError is a substrate bug and fails loudly."""
    bad = _RaisingDiscoverer(RuntimeError("genuine bug"))
    with pytest.raises(RuntimeError):
        collect(_EMPTY_MANIFEST, (bad,), name_to_source={})


# --- grounding ---------------------------------------------------------------


def test_grounding_opaque_scope_is_explicit_top() -> None:
    """A scope in the opaque set grounds EXPLICIT-top regardless of any facts present."""
    facts = {_COL_A: (_fact(_COL_A, "A"),)}
    ground = grounding(facts, opaque={_COL_A}, lat=_FLAT)
    assert ground(_COL_A) == Annotation(_TOP, Opacity.EXPLICIT)


def test_grounding_resolves_fact_to_concrete() -> None:
    facts = {_COL_A: (_fact(_COL_A, "A"),)}
    ground = grounding(facts, opaque=set(), lat=_FLAT)
    assert ground(_COL_A) == Annotation("A", Opacity.CONCRETE)


def test_grounding_absent_scope_is_implicit_top() -> None:
    empty: dict[ColumnRef, tuple[Fact[str, ColumnRef], ...]] = {}
    ground = grounding(empty, opaque=set[ColumnRef](), lat=_FLAT)
    assert ground(_COL_B) == Annotation(_TOP, Opacity.IMPLICIT)


def test_grounding_contradiction_raises_build_issue() -> None:
    facts = {_COL_A: (_fact(_COL_A, "A"), _fact(_COL_A, "B"))}
    with pytest.raises(FactConflictError):
        grounding(facts, opaque=set(), lat=_FLAT)
