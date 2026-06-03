"""Metamorphic PBT: nullability is monotone in its source assignments.

In the taint order NON_NULL < UNKNOWN < NULLABLE, making a source column more
null must never make a downstream column less null. Every nullability transfer
is monotone in that order, so the whole walk must be. This oracle shares no
branch logic with the propagator, so a failure is a real non-monotone transfer
or fold bug, not a restated branch. SQL shapes are reused from the end-to-end
scenario generator; the assignment pair is drawn already ordered.
"""

from __future__ import annotations

from collections.abc import Mapping

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.lineage import propagate
from dblect.lineage.builder import build_manifest_graph, build_model_graph
from dblect.lineage.facts.model import Annotation, Opacity
from dblect.lineage.facts.property import Property, column_property
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties import Nullability, nullability
from dblect.lineage.properties.nullability import NULLABILITY_LATTICE
from tests.lineage.test_pbt_lineage import (
    CTEScenario,
    Scenario,
    build_cte_sql,
    build_manifest,
    cte_scenario,
    leaf_source_ref,
    lineage_scenario,
)

_WRAP_ALL = ("none", "coalesce", "case", "aggregate")

# Taint order: less null is smaller. CONTRADICTION should never surface; ranking
# it below everything turns a stray one into a failure, not a masking KeyError.
_TAINT_RANK: dict[Nullability, int] = {
    Nullability.CONTRADICTION: -1,
    Nullability.NON_NULL: 0,
    Nullability.UNKNOWN: 1,
    Nullability.NULLABLE: 2,
}
_OPERATIONAL = (Nullability.NON_NULL, Nullability.UNKNOWN, Nullability.NULLABLE)


def _rule_property(
    assignment: dict[ColumnRef, Nullability],
) -> Property[Nullability, ColumnRef]:
    """Nullability grounded by an explicit per-source-column assignment: a named
    column anchors REFINED on its value, everything else stays IMPLICIT UNKNOWN."""

    def ground(col: ColumnRef) -> Annotation[Nullability]:
        value = assignment.get(col, Nullability.UNKNOWN)
        if value is Nullability.UNKNOWN:
            return Annotation(Nullability.UNKNOWN, Opacity.IMPLICIT)
        return Annotation(value, Opacity.REFINED)

    return column_property(
        name=nullability.name,
        lattice=NULLABILITY_LATTICE,
        operators=nullability.operators,
        aggregates=nullability.aggregates,
        ground=ground,
        semiring=nullability.semiring,
    )


@st.composite
def _scenario_with_ordered_assignments(
    draw: st.DrawFn,
) -> tuple[Scenario, dict[ColumnRef, Nullability], dict[ColumnRef, Nullability]]:
    scenario = draw(lineage_scenario())
    lo: dict[ColumnRef, Nullability] = {}
    hi: dict[ColumnRef, Nullability] = {}
    for leaf in scenario.leaves:
        src = leaf_source_ref(scenario, leaf.name)
        for col in leaf.columns:
            ref = ColumnRef(src, col.lower())
            a = draw(st.sampled_from(_OPERATIONAL))
            b = draw(st.sampled_from(_OPERATIONAL))
            lo[ref], hi[ref] = sorted((a, b), key=lambda v: _TAINT_RANK[v])
    return scenario, lo, hi


@given(_scenario_with_ordered_assignments())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_nullability_is_monotone_in_source_assignments(
    case: tuple[Scenario, dict[ColumnRef, Nullability], dict[ColumnRef, Nullability]],
) -> None:
    scenario, lo, hi = case
    result = build_manifest_graph(build_manifest(scenario))
    assert result.issues == ()
    anns_lo = propagate(result.graph, _rule_property(lo))
    anns_hi = propagate(result.graph, _rule_property(hi))
    _assert_monotone(anns_lo, anns_hi)


@st.composite
def _cte_with_ordered_assignments(
    draw: st.DrawFn,
) -> tuple[CTEScenario, dict[ColumnRef, Nullability], dict[ColumnRef, Nullability]]:
    """A CTE scenario plus an ordered leaf-assignment pair. The COALESCE/CASE/SUM
    wrappings exercise the COALESCE transfer, not just passthrough and SUM."""
    scenario = draw(cte_scenario(multi_source=True, wrap_choices=_WRAP_ALL))
    src = SourceRef(SourceKind.SOURCE, "source.test.raw.leaf_0")
    lo: dict[ColumnRef, Nullability] = {}
    hi: dict[ColumnRef, Nullability] = {}
    for col in scenario.leaf.columns:
        ref = ColumnRef(src, col.lower())
        a = draw(st.sampled_from(_OPERATIONAL))
        b = draw(st.sampled_from(_OPERATIONAL))
        lo[ref], hi[ref] = sorted((a, b), key=lambda v: _TAINT_RANK[v])
    return scenario, lo, hi


@given(_cte_with_ordered_assignments())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_nullability_is_monotone_through_cte_wrappings(
    case: tuple[CTEScenario, dict[ColumnRef, Nullability], dict[ColumnRef, Nullability]],
) -> None:
    scenario, lo, hi = case
    sql = build_cte_sql(scenario)
    src = SourceRef(SourceKind.SOURCE, "source.test.raw.leaf_0")
    graph = build_model_graph(
        model_uid="model.test.m",
        sql=sql,
        name_to_source={scenario.leaf.name: src},
        schema={scenario.leaf.name: dict.fromkeys(scenario.leaf.columns, "INT")},
    )
    anns_lo = propagate(graph, _rule_property(lo))
    anns_hi = propagate(graph, _rule_property(hi))
    _assert_monotone(anns_lo, anns_hi, sql=sql)


def _assert_monotone(
    anns_lo: Mapping[ColumnRef, Annotation[Nullability]],
    anns_hi: Mapping[ColumnRef, Annotation[Nullability]],
    *,
    sql: str | None = None,
) -> None:
    for col, ann_lo in anns_lo.items():
        ann_hi = anns_hi[col]
        context = f" sql={sql!r}" if sql is not None else ""
        assert _TAINT_RANK[ann_hi.value] >= _TAINT_RANK[ann_lo.value], (
            f"non-monotone at {col.source.unique_id}:{col.column}: "
            f"lo-sources -> {ann_lo.value}, hi-sources -> {ann_hi.value}{context}"
        )
