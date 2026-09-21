"""The property kit: the derived rules and the constructors above
``column_property``/``relation_property``.

Uses the subset lattice (meet = intersection, join = union, top = the universe,
bottom = the empty set) throughout, the same bona-fide bounded lattice
``test_facts_lattice.py`` and ``test_propagator.py`` use, so these pin the kit's
own contract rather than any one property's semantics. The grounding fold's own
rules (a declared fact grounds CONCRETE, an absent scope is IMPLICIT top, a
contradiction is reported) are ``test_facts_grounding.py``'s to pin; here only
what the kit adds on top is tested.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import sqlglot.expressions as exp

from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.kit import (
    GroundingFold,
    column_kit,
    constant_aggregate,
    grounding_fold,
    top_rule,
)
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import Annotation, Declared, DeclaredSource, Fact, Opacity
from dblect.lineage.facts.property import DepContext
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.property import propagate
from dblect.manifest import Manifest
from tests._manifest_builders import manifest as _manifest

_UNIVERSE = frozenset({0, 1, 2, 3})
_Set = frozenset[int]


def _subset_lattice() -> Lattice[_Set]:
    return Lattice(
        meet=lambda a, b: a & b, join=lambda a, b: a | b, top=_UNIVERSE, bottom=frozenset()
    )


class _NoDeps:
    """A ``DepContext`` no rule under test actually reads: every property kit rule
    exercised here ignores its dependency context, so a context that always
    answers "nothing known" stands in without a real registry/store."""

    def annotation(self, ref: object, scope: object) -> None:
        return None


_NO_DEPS: DepContext = _NoDeps()

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.orders")
_COL = ColumnRef(_SRC, "id")
_EMPTY_MANIFEST = _manifest()


def _fact(scope: ColumnRef, value: _Set) -> Fact[_Set, ColumnRef]:
    return Fact(scope=scope, value=value, provenance=Declared(DeclaredSource.DBT_GENERIC_TEST))


def test_top_rule_is_implicit_top_and_carries_provisional_through() -> None:
    rule = top_rule(_subset_lattice())
    clean = (Annotation(frozenset({0})), Annotation(frozenset({1})))
    assert rule(exp.EQ(), clean, _NO_DEPS) == Annotation(_UNIVERSE, Opacity.IMPLICIT)
    tainted = (Annotation(frozenset({0}), provisional=True), Annotation(frozenset({1})))
    assert rule(exp.EQ(), tainted, _NO_DEPS).provisional


def test_constant_aggregate_discards_the_child_value_but_keeps_its_taint() -> None:
    rule = constant_aggregate(frozenset({7}))
    out = rule.core(exp.Count(), Annotation(frozenset({0, 1})))
    assert out == Annotation(frozenset({7}), Opacity.CONCRETE)
    assert rule.core(exp.Count(), Annotation(frozenset({0}), provisional=True)).provisional


def test_grounding_fold_preprocess_runs_before_every_reader() -> None:
    """The one hook a property passes explicitly (functional-dependency's
    declared-instance lift on main) runs on the facts before grounding, scoping,
    and conflict-scanning alike, so the three readers can never disagree."""

    def widen(facts: Mapping[ColumnRef, tuple[Fact[_Set, ColumnRef], ...]]):
        return {
            scope: tuple(Fact(f.scope, f.value | {2}, f.provenance) for f in bucket)
            for scope, bucket in facts.items()
        }

    fold: GroundingFold[_Set, ColumnRef] = grounding_fold(_subset_lattice(), preprocess=widen)
    facts = {_COL: (_fact(_COL, frozenset({0})),)}
    assert fold.ground(facts)(_COL).value == frozenset({0, 2})
    assert fold.scopes(facts) == {_COL}
    assert fold.conflicts({_COL: (_fact(_COL, frozenset()),)}) == ()


class _StaticDiscoverer:
    def __init__(self, facts: tuple[Fact[_Set, ColumnRef], ...]) -> None:
        self._facts = facts

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[_Set, ColumnRef]]:
        return self._facts


def test_column_kit_facts_collects_discoverers_and_extra_facts_into_one_map() -> None:
    kit = column_kit(name="k", lattice=_subset_lattice(), operators={}, aggregates={})
    discoverer = _StaticDiscoverer((_fact(_COL, frozenset({0})),))
    facts = kit.facts(_EMPTY_MANIFEST, (discoverer,), extra_facts=(_fact(_COL, frozenset({1})),))
    assert {f.value for f in facts[_COL]} == {frozenset({0}), frozenset({1})}


def test_column_kit_property_grounds_and_propagates() -> None:
    """The kit's ``.property`` derives a working Property: a leaf grounds its
    declared value, and a rename carries it through, the same as one built by
    hand from ``column_property`` with the same pieces."""
    kit = column_kit(name="k", lattice=_subset_lattice(), operators={}, aggregates={})
    facts = {_COL: (_fact(_COL, frozenset({0})),)}
    prop = kit.property(facts)
    graph = build_model_graph(
        model_uid="model.shop.m",
        sql="SELECT o.id AS id FROM orders o",
        name_to_source={"orders": _SRC},
        schema={"orders": {"id": "INT"}},
    )
    anns = propagate(graph, prop)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.shop.m"), "id")
    assert anns[out].value == frozenset({0})
