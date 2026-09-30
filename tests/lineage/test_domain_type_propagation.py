"""Grounding and propagation for the domain-type property.

Each source column grounds from a synthetic domain-type fact (the typed contract
bridge is a later build); the propagator then carries the tag through projections,
arithmetic, confluences, and aggregates by the algebra rules. The table pins the
transfer contracts at the boundary: same-currency arithmetic stays typed, mixed
currency contradicts, a same-currency ratio cancels, a confluence widens
disagreement, and a companion binding rides through a projection chain so a
downstream coherence guard can later read it.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    CONFLICT,
    NAKED,
    Concrete,
    Dimension,
    DomainTag,
    PerRow,
    domain_type_grounding,
    domain_type_property,
    tagged,
)
from tests.lineage._propagation_table import PropagationCase, run_propagation_case

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.charges")
_MODEL = SourceRef(SourceKind.MODEL, "model.shop.m")
_CURRENCY_COL = ColumnRef(_SRC, "currency")

_USD = tagged(dimension=Dimension.of(Concrete("usd")))
_EUR = tagged(dimension=Dimension.of(Concrete("eur")))
_DIMENSIONLESS = tagged(dimension=Dimension.dimensionless())
_PER_ROW = tagged(dimension=Dimension.of(PerRow(_CURRENCY_COL)))


def _facts(
    by_column: Mapping[str, DomainTag],
) -> Mapping[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]]:
    out: dict[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]] = {}
    for column, value in by_column.items():
        ref = ColumnRef(_SRC, column)
        out[ref] = (
            Fact(scope=ref, value=value, provenance=Declared(DeclaredSource.USER_ASSERTED)),
        )
    return out


def _run(sql: str, facts: Mapping[str, DomainTag]) -> Mapping[str, DomainTag]:
    graph = build_model_graph(
        model_uid=_MODEL.unique_id,
        sql=sql,
        name_to_source={"charges": _SRC},
        schema={"charges": {"a": "DECIMAL", "b": "DECIMAL", "amount": "DECIMAL", "k": "INT"}},
    )
    prop = domain_type_property(domain_type_grounding(_facts(facts)))
    anns = propagate(graph, prop)
    return {ref.column: ann.value for ref, ann in anns.items() if ref.source == _MODEL}


# --- grounding -----------------------------------------------------------------


def test_grounded_leaf_carries_its_declared_tag() -> None:
    facts = _facts({"amount": _USD})
    leaf = ColumnRef(_SRC, "amount")
    ground = domain_type_grounding(facts)
    assert ground(leaf).value == _USD


def test_undeclared_column_grounds_naked() -> None:
    ground = domain_type_grounding(_facts({}))
    assert ground(ColumnRef(_SRC, "amount")).value == NAKED


# --- propagation -----------------------------------------------------------------

_CASES: tuple[PropagationCase[DomainTag], ...] = (
    PropagationCase(
        "rename_preserves_the_tag",
        "SELECT c.amount AS total FROM charges c",
        "total",
        _USD,
        {"amount": _USD},
    ),
    PropagationCase(
        "companion_binding_rides_through_a_projection_chain",
        # The per-row currency binding must survive projection even after the
        # currency column itself is dropped, so a downstream coherence guard can
        # still read the binding it has to discharge.
        "SELECT c.amount AS amount FROM charges c",
        "amount",
        _PER_ROW,
        {"amount": _PER_ROW},
    ),
    PropagationCase(
        "same_currency_addition_stays_typed",
        "SELECT c.a + c.b AS total FROM charges c",
        "total",
        _USD,
        {"a": _USD, "b": _USD},
    ),
    PropagationCase(
        "mixed_currency_addition_is_a_conflict",
        "SELECT c.a + c.b AS total FROM charges c",
        "total",
        CONFLICT,
        {"a": _USD, "b": _EUR},
    ),
    PropagationCase(
        "adding_a_naked_operand_widens_to_naked",
        # A magnitude added to a column making no dimensional claim can no longer
        # be claimed to carry the magnitude's unit: the sum widens to NAKED
        # rather than inheriting the currency (the lenient resolution; strict
        # mode would call the untagged addend a finding). ``amount`` is left
        # ungrounded, so it grounds naked.
        "SELECT c.a + c.amount AS total FROM charges c",
        "total",
        NAKED,
        {"a": _USD},
    ),
    PropagationCase(
        "mixed_currency_conflict_survives_a_later_naked_addend",
        # The currency mix conflicts on the spot; a later naked addend cannot
        # launder that conflict back to a clean tag.
        "SELECT c.a + c.b + c.amount AS total FROM charges c",
        "total",
        CONFLICT,
        {"a": _USD, "b": _EUR},
    ),
    PropagationCase(
        "literal_added_to_money_keeps_currency",
        # A bare numeric literal is polymorphic: it takes the unit of what it is
        # added to, so amount + 5 stays the amount's currency.
        "SELECT c.a + 5 AS total FROM charges c",
        "total",
        _USD,
        {"a": _USD},
    ),
    PropagationCase(
        "same_currency_ratio_cancels_to_dimensionless",
        "SELECT c.a / c.b AS ratio FROM charges c",
        "ratio",
        _DIMENSIONLESS,
        {"a": _USD, "b": _USD},
    ),
    PropagationCase(
        "scalar_multiply_keeps_currency",
        "SELECT c.a * 0.9 AS scaled FROM charges c",
        "scaled",
        _USD,
        {"a": _USD},
    ),
    PropagationCase(
        "money_times_money_is_squared",
        "SELECT c.a * c.b AS prod FROM charges c",
        "prod",
        tagged(dimension=Dimension.of(Concrete("usd"), 2)),
        {"a": _USD, "b": _USD},
    ),
    PropagationCase(
        "multiplying_by_a_naked_value_does_not_remint_a_tag",
        # A no-claim operand is an unknown factor, not a dimensionless scalar: it
        # may carry hidden units. (a + b) with a untagged widens to naked, and
        # multiplying that by the typed b must stay naked rather than re-claiming
        # the currency (the empirical-soundness PBT surfaced this: the rescaling
        # law fails if the product claims a clean dimension here).
        "SELECT (c.a + c.b) * c.b AS prod FROM charges c",
        "prod",
        NAKED,
        {"b": _USD},
    ),
    PropagationCase(
        "union_of_matching_currencies_stays_typed",
        "SELECT c.a AS amt FROM charges c UNION ALL SELECT c.b AS amt FROM charges c",
        "amt",
        _USD,
        {"a": _USD, "b": _USD},
    ),
    PropagationCase(
        "union_of_differing_currencies_widens_to_naked",
        "SELECT c.a AS amt FROM charges c UNION ALL SELECT c.b AS amt FROM charges c",
        "amt",
        NAKED,
        {"a": _USD, "b": _EUR},
    ),
    PropagationCase(
        "sum_passes_the_tag_through",
        # Without a discharge the soundness of the sum is the coherence guard's
        # concern; the pure value-domain map keeps the tag for the guard to judge.
        "SELECT SUM(c.a) AS total FROM charges c",
        "total",
        _USD,
        {"a": _USD},
    ),
    PropagationCase(
        "count_is_tag_free", "SELECT COUNT(c.a) AS n FROM charges c", "n", NAKED, {"a": _USD}
    ),
    PropagationCase(
        "min_preserves_the_tag", "SELECT MIN(c.a) AS lo FROM charges c", "lo", _USD, {"a": _USD}
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
def test_domain_type_propagation(case: PropagationCase[DomainTag]) -> None:
    run_propagation_case(case, _run)
