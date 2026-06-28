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

from dblect.lineage.builder import build_model_graph, build_relation_graph
from dblect.lineage.facts.model import Annotation, Declared, DeclaredSource, Fact, Opacity
from dblect.lineage.facts.property import CoherenceClear, DischargePath
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import ColumnLineageGraph, ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    NAKED,
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
from dblect.manifest import Manifest, Node, ResourceType

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


def _node(ref: SourceRef, sql: str | None) -> Node:
    kind = ResourceType.MODEL if ref.kind is SourceKind.MODEL else ResourceType.SOURCE
    return Node(
        unique_id=ref.unique_id,
        name=ref.unique_id.split(".")[-1],
        resource_type=kind,
        fqn=(ref.unique_id,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
    )


def _propagate(
    sql: str,
    *,
    amount: DomainTag = _PER_ROW,
    fds: FDSet = NO_FDS,
    stg_sql: str | None = None,
) -> tuple[Mapping[ColumnRef, Annotation[DomainTag]], tuple[CoherenceClear[DomainTag], ...]]:
    """Propagate functional dependencies over the relation graph, then domain type
    over the column graph with the FD store as its dependency context, returning every
    column's annotation alongside the coherence clears the guard emitted into the sink."""
    nodes = [_node(_SRC, None), _node(_CUSTOMERS, None), _node(_MODEL, sql)]
    if stg_sql is not None:
        nodes.append(_node(_STG, stg_sql))
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )

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
    dt_prop = domain_type_property(domain_type_grounding(dt_facts), fd=fd_prop.ref)
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
) -> Annotation[DomainTag]:
    """The aggregate output column ``out`` of the leaf model after propagation."""
    anns, _ = _propagate(sql, amount=amount, fds=fds, stg_sql=stg_sql)
    return anns[ColumnRef(_MODEL, out)]


def _clears(
    sql: str,
    *,
    amount: DomainTag = _PER_ROW,
    fds: FDSet = NO_FDS,
    stg_sql: str | None = None,
) -> tuple[CoherenceClear[DomainTag], ...]:
    """The coherence clears the guard emitted while propagating ``sql``."""
    _, clears = _propagate(sql, amount=amount, fds=fds, stg_sql=stg_sql)
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
