"""The aggregate coherence guard: a sum over a per-row tag clears unless discharged.

This is the headline aggregation contract from the currency story. ``amount``
carries a per-row currency binding (a ``Money`` whose unit is the companion
``currency`` column), and ``SUM(amount) GROUP BY country`` is meaningful only when
the currency is constant within each group. The discharge paths are exactly the
three the algebra admits: the companion is in the group key, the companion is
pinned (a literal binding, or an equality filter in the aggregating scope), or the
group key functionally determines the companion (a ``country -> currency``
dependency read from the FD property). Where no path discharges, the aggregate
clears to the lattice top, which is what a downstream seam reports as the
mixed-currency-sum finding.

The guard's posture everywhere it cannot see is silent-when-unproven: a join
input, a windowed aggregate, or a companion bound to a column of some other
relation all clear rather than guess.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from dblect.lineage.builder import build_model_graph, build_relation_graph
from dblect.lineage.facts.model import Annotation, Declared, DeclaredSource, Fact, Opacity
from dblect.lineage.facts.property import CoherenceClear, DischargePath, KeyVerdict
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    NAKED,
    CompanionFacts,
    Concrete,
    Dimension,
    DomainTag,
    PerRow,
    domain_type_grounding,
    domain_type_property,
    tagged,
)
from dblect.lineage.properties.functional_dependency import (
    FD,
    NO_FDS,
    FDSet,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import Node, ResourceType
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.payments")
_CUSTOMERS = SourceRef(SourceKind.SOURCE, "source.shop.raw.customers")
_STG = SourceRef(SourceKind.MODEL, "model.shop.stg")
_MODEL = SourceRef(SourceKind.MODEL, "model.shop.m")

_PER_ROW = tagged(dimension=Dimension.of(PerRow(ColumnRef(_SRC, "currency"))))
_USD = tagged(dimension=Dimension.of(Concrete("usd")))

_SCHEMA: Mapping[str, Mapping[str, str]] = {
    "payments": {
        "amount": "DECIMAL",
        "currency": "VARCHAR",
        "country": "VARCHAR",
        "customer_id": "INT",
    },
    "customers": {"id": "INT", "region": "VARCHAR"},
    "stg": {"amount": "DECIMAL", "currency": "VARCHAR", "country": "VARCHAR"},
}
_NAME_TO_SOURCE: Mapping[str, SourceRef] = {
    "payments": _SRC,
    "customers": _CUSTOMERS,
    "stg": _STG,
}


def _relation(ref: SourceRef, sql: str | None) -> Node:
    kind = ResourceType.MODEL if ref.kind is SourceKind.MODEL else ResourceType.SOURCE
    return _node(ref.unique_id, sql, kind=kind)


def _propagate(
    sql: str,
    *,
    amount: DomainTag = _PER_ROW,
    fds: FDSet = NO_FDS,
    stg_sql: str | None = None,
    companions: CompanionFacts | None = None,
) -> tuple[Mapping[ColumnRef, Annotation[DomainTag]], tuple[CoherenceClear[DomainTag], ...]]:
    """Propagate functional dependencies over the relation graph, then domain type
    over the column graph with the FD store as its dependency context, returning every
    column's annotation alongside the coherence clears the guard emitted into the sink."""
    nodes = [_relation(_SRC, None), _relation(_CUSTOMERS, None), _relation(_MODEL, sql)]
    if stg_sql is not None:
        nodes.append(_relation(_STG, stg_sql))
    manifest = _manifest(*nodes)

    fd_fact = Fact(scope=_SRC, value=fds, provenance=Declared(DeclaredSource.USER_ASSERTED))
    fd_prop = functional_dependency_property(functional_dependency_grounding({_SRC: (fd_fact,)}))
    store = AnnotationStore()
    for scope, ann in propagate(build_relation_graph(manifest).graph, fd_prop).items():
        store.record(fd_prop.name, scope, ann)

    amount_ref = ColumnRef(_SRC, "amount")
    dt_facts = {
        amount_ref: (
            Fact(scope=amount_ref, value=amount, provenance=Declared(DeclaredSource.USER_ASSERTED)),
        )
    }
    dt_prop = domain_type_property(
        domain_type_grounding(dt_facts), fd=fd_prop.ref, companion_facts=companions
    )
    ctx = PropertyRegistry((fd_prop, dt_prop)).dep_context(store)

    graph = ColumnLineageGraph.empty()
    for ref, model_sql in ((_STG, stg_sql), (_MODEL, sql)):
        if model_sql is None:
            continue
        graph = graph.merge(
            build_model_graph(
                model_uid=ref.unique_id,
                sql=model_sql,
                name_to_source=_NAME_TO_SOURCE,
                schema=_SCHEMA,
            )
        )
    clears: list[CoherenceClear[DomainTag]] = []
    anns = propagate(graph, dt_prop, dep_context=ctx, sink=clears)
    return anns, tuple(clears)


def _run(
    sql: str,
    *,
    amount: DomainTag = _PER_ROW,
    fds: FDSet = NO_FDS,
    stg_sql: str | None = None,
    out: str = "total",
    companions: CompanionFacts | None = None,
) -> Annotation[DomainTag]:
    """The aggregate output column ``out`` of the leaf model after propagation."""
    anns, _ = _propagate(sql, amount=amount, fds=fds, stg_sql=stg_sql, companions=companions)
    return anns[ColumnRef(_MODEL, out)]


def _clears(
    sql: str,
    *,
    amount: DomainTag = _PER_ROW,
    fds: FDSet = NO_FDS,
    stg_sql: str | None = None,
    companions: CompanionFacts | None = None,
) -> tuple[CoherenceClear[DomainTag], ...]:
    """The coherence clears the guard emitted while propagating ``sql``."""
    _, clears = _propagate(sql, amount=amount, fds=fds, stg_sql=stg_sql, companions=companions)
    return clears


_HEADLINE = "SELECT country, SUM(amount) AS total FROM payments GROUP BY country"


# --- the finding ---------------------------------------------------------------


def test_undischarged_sum_clears_to_naked() -> None:
    """The headline: summing a per-row-currency amount grouped by country, with no
    dependency in sight, is not well typed; the tag clears."""
    ann = _run(_HEADLINE)
    assert ann.value == NAKED
    assert ann.opacity is Opacity.IMPLICIT  # incidental top: a seam warns on it


def test_ungrouped_sum_clears_to_naked() -> None:
    """No GROUP BY reduces over the whole relation, the strictest obligation."""
    ann = _run("SELECT SUM(amount) AS total FROM payments")
    assert ann.value == NAKED


# --- the discharges ------------------------------------------------------------


def test_declared_fd_discharges_the_sum() -> None:
    """``country -> currency`` holds each group to one currency, so the sum keeps
    its tag even though the currency column was never read."""
    fds = FDSet.of(FD(frozenset({"country"}), "currency"))
    ann = _run(_HEADLINE, fds=fds)
    assert ann.value == _PER_ROW


def test_group_by_membership_discharges_the_sum() -> None:
    sql = "SELECT country, currency, SUM(amount) AS total FROM payments GROUP BY country, currency"
    ann = _run(sql)
    assert ann.value == _PER_ROW


def test_where_pin_discharges_the_sum() -> None:
    sql = (
        "SELECT country, SUM(amount) AS total FROM payments WHERE currency = 'usd' GROUP BY country"
    )
    ann = _run(sql)
    assert ann.value == _PER_ROW


def test_where_pin_discharges_an_ungrouped_sum() -> None:
    ann = _run("SELECT SUM(amount) AS total FROM payments WHERE currency = 'usd'")
    assert ann.value == _PER_ROW


def test_constancy_fd_discharges_an_ungrouped_sum() -> None:
    """A declared ``{} -> currency`` (single-currency relation) discharges even the
    whole-relation reduction."""
    ann = _run(
        "SELECT SUM(amount) AS total FROM payments", fds=FDSet.of(FD(frozenset(), "currency"))
    )
    assert ann.value == _PER_ROW


def test_concrete_binding_needs_no_discharge() -> None:
    """A pinned literal currency is constant everywhere; the guard has nothing to ask."""
    ann = _run(_HEADLINE, amount=_USD)
    assert ann.value == _USD


# --- aggregate kinds -----------------------------------------------------------


def test_avg_is_guarded_like_sum() -> None:
    assert (
        _run("SELECT country, AVG(amount) AS total FROM payments GROUP BY country").value == NAKED
    )
    fds = FDSet.of(FD(frozenset({"country"}), "currency"))
    sql = "SELECT country, AVG(amount) AS total FROM payments GROUP BY country"
    assert _run(sql, fds=fds).value == _PER_ROW


def test_count_is_unaffected() -> None:
    ann = _run("SELECT country, COUNT(amount) AS total FROM payments GROUP BY country")
    assert ann.value == NAKED
    assert not ann.provisional


# --- shapes the guard cannot see clear conservatively ----------------------------


def test_join_input_blocks_the_fd_discharge() -> None:
    """The aggregation input is not one relation the FD property annotates, so the
    dependency path is closed; only group membership or a pin can discharge."""
    fds = FDSet.of(FD(frozenset({"country"}), "currency"))
    sql = (
        "SELECT p.country, SUM(p.amount) AS total FROM payments p "
        "JOIN customers c ON p.customer_id = c.id GROUP BY p.country"
    )
    assert _run(sql, fds=fds).value == NAKED


def test_group_membership_still_discharges_over_a_join() -> None:
    """The companion in the group key is constant per group whatever the join did;
    fan-out is the grain axis, not tag coherence."""
    sql = (
        "SELECT p.country, p.currency, SUM(p.amount) AS total FROM payments p "
        "JOIN customers c ON p.customer_id = c.id GROUP BY p.country, p.currency"
    )
    assert _run(sql).value == _PER_ROW


def test_companion_bound_to_another_relation_clears() -> None:
    """The amount reaches the aggregate through an intermediate model, so its
    companion still names the original source's column while the aggregation input
    is the intermediate. The guard does not chase bindings across relations yet, so
    it clears; rebinding the companion through projections is future work."""
    fds = FDSet.of(FD(frozenset({"country"}), "currency"))
    ann = _run(
        "SELECT country, SUM(amount) AS total FROM stg GROUP BY country",
        fds=fds,
        stg_sql="SELECT country, currency, amount FROM payments",
    )
    assert ann.value == NAKED


def test_windowed_aggregate_clears() -> None:
    """A window's partition list, not the scope's GROUP BY, is its group key; until
    the guard reads window structure it stays silent-when-unproven."""
    sql = "SELECT SUM(amount) OVER (PARTITION BY currency) AS total FROM payments"
    assert _run(sql).value == NAKED


# --- the emitted clear signal --------------------------------------------------
#
# The clear is the substrate's record of *why* the tag went to top: the guard fired
# on a live per-row companion, not the operand arriving naked. A downstream check
# reads this instead of re-inferring the event from an ambiguous ``output == NAKED``.

_CURRENCY = ColumnRef(_SRC, "currency")


def test_undischarged_sum_emits_a_clear() -> None:
    """The headline clear carries the reduced tag and the undischarged companion with
    every discharge path the guard checked and failed."""
    (clear,) = _clears(_HEADLINE)
    assert clear.cleared_value == _PER_ROW
    (undischarged,) = clear.undischarged
    assert undischarged.companion == _CURRENCY
    assert undischarged.paths_tried == frozenset(
        {DischargePath.GROUP_KEY, DischargePath.PIN, DischargePath.FD}
    )


def test_expression_operand_still_emits_a_clear() -> None:
    """``sum(amount * 2)`` keeps the per-row currency through the scalar factor, so the
    guard fires on the product just as it does on the bare column: the recall the
    bare-column restriction dropped."""
    (clear,) = _clears("SELECT country, SUM(amount * 2) AS total FROM payments GROUP BY country")
    assert clear.cleared_value == _PER_ROW
    assert {u.companion for u in clear.undischarged} == {_CURRENCY}


def test_naked_operand_emits_no_clear() -> None:
    """``sum(CASE WHEN .. THEN amount ELSE 0 END)`` mixes the magnitude with a
    dimensionless literal, so the operand is already naked before the reduction. No
    companion is live, the guard never fires, and nothing is emitted: precision the
    output-only proxy could not keep without the bare-column restriction."""
    sql = (
        "SELECT country, SUM(CASE WHEN country = 'us' THEN amount ELSE 0 END) AS total "
        "FROM payments GROUP BY country"
    )
    assert _run(sql, out="total").value == NAKED
    assert _clears(sql) == ()


def test_group_membership_discharge_emits_no_clear() -> None:
    """A discharged companion is not a clear: the tag survives and the sink stays empty."""
    sql = "SELECT country, currency, SUM(amount) AS total FROM payments GROUP BY country, currency"
    assert _clears(sql) == ()


def test_declared_fd_discharge_emits_no_clear() -> None:
    fds = FDSet.of(FD(frozenset({"country"}), "currency"))
    assert _clears(_HEADLINE, fds=fds) == ()


def test_concrete_binding_emits_no_clear() -> None:
    """A pinned literal currency has no companion, so the guard has nothing to clear."""
    assert _clears(_HEADLINE, amount=_USD) == ()


# --- group keys that determine the companion -----------------------------------
#
# A group key holds the companion constant when it determines it within a group. The
# builder reads the key's shape, the guard decides each shape against the companion's
# declared values and nullability. Every shape of the closed vocabulary is decided.

_ENUM = frozenset({"USD", "EUR"})
_CASE_COLLIDING = frozenset({"USD", "usd"})
_PADDED_COLLIDING = frozenset({" USD", "USD"})


def _facts(*, non_null: bool = False, members: frozenset[str] | None = _ENUM) -> CompanionFacts:
    return CompanionFacts(non_null=lambda _ref: non_null, members=lambda _ref: members)


def _by(
    key: str, *, non_null: bool = False, members: frozenset[str] | None = _ENUM
) -> tuple[Mapping[ColumnRef, Annotation[DomainTag]], tuple[CoherenceClear[DomainTag], ...]]:
    sql = f"SELECT {key} AS k, SUM(amount) AS total FROM payments GROUP BY {key}"
    return _propagate(sql, companions=_facts(non_null=non_null, members=members))


def _holds(key: str, *, non_null: bool = False, members: frozenset[str] | None = _ENUM) -> bool:
    _, clears = _by(key, non_null=non_null, members=members)
    return not clears


@pytest.mark.parametrize(
    ("key", "non_null", "members", "holds"),
    [
        ("currency", False, _ENUM, True),
        ("(currency)", False, _ENUM, True),
        # COALESCE: the head column is determined on its non-null rows only.
        ("coalesce(currency, 'USD')", True, _ENUM, True),
        ("coalesce(currency, 'USD')", False, _ENUM, False),
        ("coalesce('USD', currency)", True, _ENUM, False),
        ("coalesce(upper(currency), 'USD')", True, _ENUM, False),
        # UPPER / LOWER / TRIM: only when injective on the declared members.
        ("upper(currency)", False, _ENUM, True),
        ("upper(currency)", False, _CASE_COLLIDING, False),
        ("upper(currency)", False, None, False),
        ("upper(currency)", False, frozenset({"é", "E"}), False),
        ("lower(currency)", False, _ENUM, True),
        ("lower(currency)", False, _CASE_COLLIDING, False),
        ("trim(currency)", False, _ENUM, True),
        ("trim(currency)", False, _PADDED_COLLIDING, False),
        ("trim(currency, 'U')", False, _ENUM, False),
        # CAST: a cast to unsized text is the identity on string members; every other
        # target (numbers, dates) makes no claim. Sized targets are pinned in test_vocab.
        ("cast(currency AS varchar)", False, _ENUM, True),
        ("cast(currency AS text)", False, _ENUM, True),
        ("cast(currency AS varchar)", False, None, False),
        ("cast(currency AS integer)", False, _ENUM, False),
        # Everything else makes no claim.
        ("CASE WHEN country = 'us' THEN currency END", True, _ENUM, False),
        ("substr(currency, 1, 2)", True, _ENUM, False),
        ("currency || country", True, _ENUM, False),
    ],
)
def test_group_key_holds_the_companion_per_shape(
    key: str, non_null: bool, members: frozenset[str] | None, holds: bool
) -> None:
    assert _holds(key, non_null=non_null, members=members) is holds


def test_a_positional_group_key_is_read_through_the_projection() -> None:
    sql = "SELECT coalesce(currency, 'USD') AS k, SUM(amount) AS total FROM payments GROUP BY 1"
    _, clears = _propagate(sql, companions=_facts(non_null=True))
    assert clears == ()


def test_an_unrelated_key_beside_a_holding_key_does_not_block_it() -> None:
    sql = (
        "SELECT country, upper(currency) AS k, SUM(amount) AS total FROM payments "
        "GROUP BY country, upper(currency)"
    )
    _, clears = _propagate(sql, companions=_facts())
    assert clears == ()


def test_a_key_over_another_column_does_not_hold_the_companion() -> None:
    assert not _holds("upper(country)")


def test_coalesce_over_an_outer_join_padded_side_does_not_hold() -> None:
    """``payments`` is the optional side, so its NOT NULL currency is NULL on the rows
    the join pads; the fallback then attributes those rows a currency they do not have."""
    padded = (
        "SELECT coalesce(p.currency, 'USD') AS k, SUM(p.amount) AS total FROM customers c "
        "LEFT JOIN payments p ON p.customer_id = c.id GROUP BY 1"
    )
    preserved = (
        "SELECT coalesce(p.currency, 'USD') AS k, SUM(p.amount) AS total FROM payments p "
        "LEFT JOIN customers c ON p.customer_id = c.id GROUP BY 1"
    )
    facts = _facts(non_null=True)
    assert _propagate(padded, companions=facts)[1] != ()
    assert _propagate(preserved, companions=facts)[1] == ()


# What the finding says about why a key failed, per verdict.


def _verdicts(
    key: str, *, non_null: bool = False, members: frozenset[str] | None = _ENUM
) -> list[tuple[str, KeyVerdict]]:
    _, clears = _by(key, non_null=non_null, members=members)
    (clear,) = clears
    (undischarged,) = clear.undischarged
    return [(b.key.sql, b.verdict) for b in undischarged.blocked]


def test_a_nullable_fallback_names_the_key_and_the_null_fallback() -> None:
    assert _verdicts("coalesce(currency, 'USD')", non_null=False) == [
        ("COALESCE(payments.currency, 'USD')", KeyVerdict.NULL_FALLBACK)
    ]


def test_a_colliding_wrapper_is_named_as_colliding() -> None:
    assert _verdicts("upper(currency)", members=_CASE_COLLIDING) == [
        ("UPPER(payments.currency)", KeyVerdict.COLLIDES)
    ]


def test_a_wrapper_with_no_declared_members_is_named_as_unknown_domain() -> None:
    assert _verdicts("lower(currency)", members=None) == [
        ("LOWER(payments.currency)", KeyVerdict.UNKNOWN_DOMAIN)
    ]


def test_an_unrecognised_key_is_named_as_opaque() -> None:
    assert _verdicts("substr(currency, 1, 2)") == [
        ("SUBSTRING(payments.currency, 1, 2)", KeyVerdict.OPAQUE)
    ]
