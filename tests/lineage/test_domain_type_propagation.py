"""Grounding and propagation for the domain-type property.

Each source column grounds from a synthetic domain-type fact (the typed contract
bridge is a later build); the propagator then carries the tag through projections,
arithmetic, confluences, and aggregates by the algebra rules. These pin the
transfer contracts at the boundary: same-currency arithmetic stays typed, mixed
currency contradicts, a same-currency ratio cancels, a confluence widens
disagreement, and a companion binding rides through a projection chain so a
downstream coherence guard can later read it.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Annotation, Declared, DeclaredSource, Fact
from dblect.lineage.facts.property import Property
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

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.charges")
_MODEL = SourceRef(SourceKind.MODEL, "model.shop.m")

_USD = tagged(dimension=Dimension.of(Concrete("usd")))
_EUR = tagged(dimension=Dimension.of(Concrete("eur")))
_DIMENSIONLESS = tagged(dimension=Dimension.dimensionless())


def _facts(**by_column: DomainTag) -> Mapping[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]]:
    out: dict[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]] = {}
    for column, value in by_column.items():
        ref = ColumnRef(_SRC, column)
        out[ref] = (
            Fact(scope=ref, value=value, provenance=Declared(DeclaredSource.USER_ASSERTED)),
        )
    return out


def _prop(
    facts: Mapping[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]],
) -> Property[DomainTag, ColumnRef]:
    return domain_type_property(domain_type_grounding(facts))


def _run(
    sql: str, facts: Mapping[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]], *, out: str
) -> Annotation[DomainTag]:
    graph = build_model_graph(
        model_uid=_MODEL.unique_id,
        sql=sql,
        name_to_source={"charges": _SRC},
        schema={"charges": {"a": "DECIMAL", "b": "DECIMAL", "amount": "DECIMAL", "k": "INT"}},
    )
    anns = propagate(graph, _prop(facts))
    return anns[ColumnRef(_MODEL, out)]


# --- grounding ---------------------------------------------------------------


def test_grounded_leaf_carries_its_declared_tag() -> None:
    facts = _facts(amount=_USD)
    leaf = ColumnRef(_SRC, "amount")
    ground = domain_type_grounding(facts)
    assert ground(leaf).value == _USD


def test_undeclared_column_grounds_naked() -> None:
    ground = domain_type_grounding(_facts())
    assert ground(ColumnRef(_SRC, "amount")).value == NAKED


# --- passthrough and companion binding ---------------------------------------


def test_rename_preserves_the_tag() -> None:
    ann = _run("SELECT c.amount AS total FROM charges c", _facts(amount=_USD), out="total")
    assert ann.value == _USD


def test_companion_binding_rides_through_a_projection_chain() -> None:
    """A per-row currency binding references the upstream ``currency`` column and
    must survive projection even after that column is dropped, so a downstream
    coherence guard can still read the binding it has to discharge."""
    currency_col = ColumnRef(_SRC, "currency")
    per_row = tagged(dimension=Dimension.of(PerRow(currency_col)))
    ann = _run("SELECT c.amount AS amount FROM charges c", _facts(amount=per_row), out="amount")
    assert ann.value == per_row


# --- additive arithmetic -----------------------------------------------------


def test_same_currency_addition_stays_typed() -> None:
    ann = _run("SELECT c.a + c.b AS total FROM charges c", _facts(a=_USD, b=_USD), out="total")
    assert ann.value == _USD


def test_mixed_currency_addition_is_a_conflict() -> None:
    ann = _run("SELECT c.a + c.b AS total FROM charges c", _facts(a=_USD, b=_EUR), out="total")
    assert ann.value is CONFLICT


def test_adding_a_naked_operand_widens_to_naked() -> None:
    """A magnitude added to a column that makes no dimensional claim can no longer be
    claimed to carry the magnitude's unit: the unknown addend could be anything, so the
    sum widens to ``NAKED`` rather than inheriting the currency (the lenient resolution;
    strict mode would call the untagged addend a finding). ``amount`` is left ungrounded,
    so it grounds naked."""
    ann = _run("SELECT c.a + c.amount AS total FROM charges c", _facts(a=_USD), out="total")
    assert ann.value == NAKED


def test_mixed_currency_conflict_survives_a_later_naked_addend() -> None:
    """The currency mix is the finding even when a no-claim addend follows it: the
    ``c.a + c.b`` node conflicts on the spot, and adding the naked ``amount`` cannot
    launder that conflict back to a clean tag."""
    ann = _run(
        "SELECT c.a + c.b + c.amount AS total FROM charges c", _facts(a=_USD, b=_EUR), out="total"
    )
    assert ann.value is CONFLICT


def test_literal_added_to_money_keeps_currency() -> None:
    """A bare numeric literal is polymorphic: it takes the unit of what it is added to,
    so ``amount + 5`` stays the amount's currency rather than conflicting on a scalar.
    This is what keeps the additive no-claim rule from firing on a literal, which is a
    no-claim value of a different kind (a known scalar) than an untagged column."""
    ann = _run("SELECT c.a + 5 AS total FROM charges c", _facts(a=_USD), out="total")
    assert ann.value == _USD


# --- multiplicative arithmetic -----------------------------------------------


def test_same_currency_ratio_cancels_to_dimensionless() -> None:
    ann = _run("SELECT c.a / c.b AS ratio FROM charges c", _facts(a=_USD, b=_USD), out="ratio")
    assert ann.value == _DIMENSIONLESS
    assert ann.value != NAKED


def test_scalar_multiply_keeps_currency() -> None:
    ann = _run("SELECT c.a * 0.9 AS scaled FROM charges c", _facts(a=_USD), out="scaled")
    assert ann.value == _USD


def test_money_times_money_is_squared() -> None:
    ann = _run("SELECT c.a * c.b AS prod FROM charges c", _facts(a=_USD, b=_USD), out="prod")
    assert ann.value == tagged(dimension=Dimension.of(Concrete("usd"), 2))


def test_multiplying_by_a_naked_value_does_not_remint_a_tag() -> None:
    """A no-claim operand is an *unknown* factor, not a dimensionless scalar: it may
    carry hidden units. ``(a + b)`` with ``b`` typed and ``a`` untagged widens to
    naked, and multiplying that by the typed ``b`` must stay naked rather than
    re-claiming the currency. The empirical-soundness PBT is what surfaced this: the
    rescaling law fails if the product claims a clean dimension here."""
    ann = _run("SELECT (c.a + c.b) * c.b AS prod FROM charges c", _facts(b=_USD), out="prod")
    assert ann.value == NAKED


# --- confluence --------------------------------------------------------------


def test_union_of_matching_currencies_stays_typed() -> None:
    sql = "SELECT c.a AS amt FROM charges c UNION ALL SELECT c.b AS amt FROM charges c"
    ann = _run(sql, _facts(a=_USD, b=_USD), out="amt")
    assert ann.value == _USD


def test_union_of_differing_currencies_widens_to_naked() -> None:
    sql = "SELECT c.a AS amt FROM charges c UNION ALL SELECT c.b AS amt FROM charges c"
    ann = _run(sql, _facts(a=_USD, b=_EUR), out="amt")
    assert ann.value == NAKED


# --- aggregates --------------------------------------------------------------


def test_sum_passes_the_tag_through() -> None:
    """Without a discharge the soundness of the sum is the coherence guard's
    concern; the pure value-domain map keeps the tag for the guard to judge."""
    ann = _run("SELECT SUM(c.a) AS total FROM charges c", _facts(a=_USD), out="total")
    assert ann.value == _USD


def test_count_is_tag_free() -> None:
    ann = _run("SELECT COUNT(c.a) AS n FROM charges c", _facts(a=_USD), out="n")
    assert ann.value == NAKED


def test_min_preserves_the_tag() -> None:
    ann = _run("SELECT MIN(c.a) AS lo FROM charges c", _facts(a=_USD), out="lo")
    assert ann.value == _USD
