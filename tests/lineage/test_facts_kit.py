"""The property kit: the derived rules and the constructors above
``column_property``/``relation_property``.

Uses the subset lattice (meet = intersection, join = union, top = the universe,
bottom = the empty set) throughout, the same bona-fide bounded lattice
``test_facts_lattice.py`` and ``test_propagator.py`` use, so these pin the kit's
own contract rather than any one property's semantics.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import pytest
import sqlglot.expressions as exp

from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.kit import (
    GroundingFold,
    column_kit,
    constant_aggregate,
    grounding_fold,
    relation_kit,
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


# --- top_rule ------------------------------------------------------------------


def test_top_rule_returns_the_lattice_top_implicit() -> None:
    rule = top_rule(_subset_lattice())
    out = rule(exp.EQ(), (Annotation(frozenset({0})),), _NO_DEPS)
    assert out == Annotation(_UNIVERSE, Opacity.IMPLICIT, provisional=False)


def test_top_rule_carries_provisional_through() -> None:
    rule = top_rule(_subset_lattice())
    kids = (Annotation(frozenset({0}), provisional=True), Annotation(frozenset({1})))
    out = rule(exp.EQ(), kids, _NO_DEPS)
    assert out.provisional


def test_top_rule_with_no_children_is_not_provisional() -> None:
    rule = top_rule(_subset_lattice())
    out = rule(exp.EQ(), (), _NO_DEPS)
    assert out == Annotation(_UNIVERSE, Opacity.IMPLICIT, provisional=False)


# --- constant_aggregate ----------------------------------------------------------


def test_constant_aggregate_ignores_the_child_value() -> None:
    rule = constant_aggregate(frozenset({7}))
    out = rule.core(exp.Count(), Annotation(frozenset({0, 1})))
    assert out.value == frozenset({7})


def test_constant_aggregate_default_opacity_is_concrete() -> None:
    rule = constant_aggregate(frozenset({7}))
    out = rule.core(exp.Count(), Annotation(frozenset({0})))
    assert out.opacity is Opacity.CONCRETE


def test_constant_aggregate_carries_the_childs_provisional_taint() -> None:
    rule = constant_aggregate(frozenset({7}))
    out = rule.core(exp.Count(), Annotation(frozenset({0}), provisional=True))
    assert out.provisional


def test_constant_aggregate_carries_no_coherence_guard() -> None:
    rule = constant_aggregate(frozenset({7}))
    assert rule.coherence is None


# --- grounding_fold --------------------------------------------------------------


def test_grounding_fold_grounds_a_declared_fact() -> None:
    fold: GroundingFold[_Set, ColumnRef] = grounding_fold(_subset_lattice())
    facts = {_COL: (_fact(_COL, frozenset({0})),)}
    ground = fold.ground(facts)
    assert ground(_COL) == Annotation(frozenset({0}), Opacity.CONCRETE)


def test_grounding_fold_undeclared_scope_is_implicit_top() -> None:
    fold: GroundingFold[_Set, ColumnRef] = grounding_fold(_subset_lattice())
    ground = fold.ground({})
    assert ground(_COL) == Annotation(_UNIVERSE, Opacity.IMPLICIT)


def test_grounding_fold_scopes_reports_what_actually_grounded() -> None:
    fold: GroundingFold[_Set, ColumnRef] = grounding_fold(_subset_lattice())
    facts = {_COL: (_fact(_COL, frozenset({0})),)}
    assert fold.scopes(facts) == {_COL}


def test_grounding_fold_conflicts_reports_a_contradiction() -> None:
    fold: GroundingFold[_Set, ColumnRef] = grounding_fold(_subset_lattice())
    facts = {_COL: (_fact(_COL, frozenset({0})), _fact(_COL, frozenset({1})))}
    assert fold.conflicts(facts) == (_COL,)


def test_grounding_fold_preprocess_runs_before_the_fold() -> None:
    """The one hook a property passes explicitly (functional-dependency's
    declared-instance lift on main) runs on the facts before grounding, scoping,
    and conflict-scanning alike."""

    def widen(facts: Mapping[ColumnRef, tuple[Fact[_Set, ColumnRef], ...]]):
        return {
            scope: tuple(Fact(f.scope, f.value | {2}, f.provenance) for f in bucket)
            for scope, bucket in facts.items()
        }

    fold: GroundingFold[_Set, ColumnRef] = grounding_fold(_subset_lattice(), preprocess=widen)
    facts = {_COL: (_fact(_COL, frozenset({0})),)}
    ground = fold.ground(facts)
    assert ground(_COL).value == frozenset({0, 2})


# --- column_kit / relation_kit / PropertyKit --------------------------------------


class _StaticDiscoverer:
    def __init__(self, facts: tuple[Fact[_Set, ColumnRef], ...]) -> None:
        self._facts = facts

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[_Set, ColumnRef]]:
        return self._facts


def test_column_kit_facts_collects_from_its_discoverers() -> None:
    kit = column_kit(name="k", lattice=_subset_lattice(), operators={}, aggregates={})
    discoverer = _StaticDiscoverer((_fact(_COL, frozenset({0})),))
    facts = kit.facts(_EMPTY_MANIFEST, (discoverer,))
    assert facts[_COL][0].value == frozenset({0})


def test_column_kit_facts_folds_in_extra_facts() -> None:
    kit = column_kit(name="k", lattice=_subset_lattice(), operators={}, aggregates={})
    facts = kit.facts(_EMPTY_MANIFEST, (), extra_facts=(_fact(_COL, frozenset({1})),))
    assert facts[_COL][0].value == frozenset({1})


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


def test_column_kit_grounded_scopes_and_conflicts_delegate_to_the_fold() -> None:
    kit = column_kit(name="k", lattice=_subset_lattice(), operators={}, aggregates={})
    facts = {_COL: (_fact(_COL, frozenset({0})), _fact(_COL, frozenset({1})))}
    assert kit.conflicts(facts) == (_COL,)


def test_relation_kit_with_no_reducer_builds_a_property_with_none() -> None:
    """Mirrors ``relation_property``'s own contract: a relation kit built with no
    reducer still constructs a Property; only propagating it fails, at the
    propagator's single dispatch point rather than here."""
    kit = relation_kit(name="k", lattice=_subset_lattice(), operators={}, aggregates={})
    prop = kit.property({})
    assert prop.reducer is None


@pytest.mark.parametrize("opacity", [Opacity.CONCRETE, Opacity.IMPLICIT])
def test_constant_aggregate_opacity_is_exactly_what_was_passed(opacity: Opacity) -> None:
    rule = constant_aggregate(frozenset({1}), opacity=opacity)
    assert rule.core(exp.Count(), Annotation(frozenset())).opacity is opacity
