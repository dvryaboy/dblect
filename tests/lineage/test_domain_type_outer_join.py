"""Outer-join NULL companion widens the tag (the C1 join concern).

An outer join pads its optional side with NULL on unmatched rows, and a NULL pads the
whole row: a per-row companion travelling with the magnitude (a ``Money`` whose unit is
a companion ``currency`` column) is NULL too, so the unit is unknown there and that
claim can no longer be made. The widening is per coordinate: each ``PerRow``-bound
piece of the tag (a dimension unit, a nominal binding) is dropped, while a pinned
``Concrete`` piece survives (a NULL amount is still of its declared currency). A tag
that was all per-row widens to ``NAKED``; an inner join, or the kept side of an outer
join, pads nothing.

The domain-type property reads the same ``OuterJoinNull`` taint the nullability property
inserts, by running over the outer-join-tainted graph. The taint marks exactly the
optional-side columns, so no separate join analysis lives here. A companion bound to a
column of a relation the magnitude no longer travels with (rebinding across projections)
is the deferred gap noted in the coherence story, not exercised here.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
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
from dblect.lineage.properties.nullability import taint_outer_joins
from dblect.lineage.property import propagate
from dblect.manifest import Manifest, Node, ResourceType

_PAYMENTS = SourceRef(SourceKind.SOURCE, "source.shop.raw.payments")
_ANCHOR = SourceRef(SourceKind.SOURCE, "source.shop.raw.anchor")
_MODEL = SourceRef(SourceKind.MODEL, "model.shop.m")

_PER_ROW = tagged(dimension=Dimension.of(PerRow(ColumnRef(_PAYMENTS, "currency"))))
_USD = tagged(dimension=Dimension.of(Concrete("usd")))

_NAME_TO_SOURCE: Mapping[str, SourceRef] = {"payments": _PAYMENTS, "anchor": _ANCHOR}
_SCHEMA: Mapping[str, Mapping[str, str]] = {
    "payments": {"amount": "DECIMAL", "currency": "VARCHAR", "cust_id": "INT"},
    "anchor": {"id": "INT"},
}

# ``payments`` is the joined-in (optional) side of a LEFT join, so ``p.amount`` is tainted.
_OPTIONAL = "SELECT p.amount AS amount FROM anchor a LEFT JOIN payments p ON a.id = p.cust_id"
# ``payments`` is the kept side; the optional side is ``anchor``, so ``p.amount`` is not.
_KEPT = "SELECT p.amount AS amount FROM payments p LEFT JOIN anchor a ON p.cust_id = a.id"
# Inner join pads nothing.
_INNER = "SELECT p.amount AS amount FROM anchor a JOIN payments p ON a.id = p.cust_id"


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


def _run(sql: str, *, amount: DomainTag = _PER_ROW, out: str = "amount") -> DomainTag:
    """Propagate domain type over the outer-join-tainted column graph and read ``out``."""
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={
            n.unique_id: n
            for n in (_node(_PAYMENTS, None), _node(_ANCHOR, None), _node(_MODEL, sql))
        },
    )
    graph = build_model_graph(
        model_uid=_MODEL.unique_id, sql=sql, name_to_source=_NAME_TO_SOURCE, schema=_SCHEMA
    )
    tainted = taint_outer_joins(graph, manifest)
    amount_ref = ColumnRef(_PAYMENTS, "amount")
    facts = {
        amount_ref: (
            Fact(scope=amount_ref, value=amount, provenance=Declared(DeclaredSource.USER_ASSERTED)),
        )
    }
    anns = propagate(tainted, domain_type_property(domain_type_grounding(facts)))
    return anns[ColumnRef(_MODEL, out)].value


def test_per_row_companion_on_optional_side_widens_to_naked() -> None:
    assert _run(_OPTIONAL) == NAKED


def test_pinned_currency_on_optional_side_is_unaffected() -> None:
    """A NULL-padded amount is still of its declared currency; only a per-row companion,
    which is itself NULL on the padded row, makes the unit unknown."""
    assert _run(_OPTIONAL, amount=_USD) == _USD


def test_per_row_companion_on_kept_side_is_preserved() -> None:
    assert _run(_KEPT) == _PER_ROW


def test_inner_join_does_not_widen() -> None:
    assert _run(_INNER) == _PER_ROW


def test_widening_is_per_coordinate_pinned_dimension_survives() -> None:
    """A tag mixing a pinned dimension with a per-row nominal binding loses only the
    per-row piece: the amount is still USD on a padded row, while whether it contains
    tax (read from a NULL-padded neighbour) is unknown there."""
    mixed = tagged(
        dimension=Dimension.of(Concrete("usd")),
        nominal={"contains_tax": PerRow(ColumnRef(_PAYMENTS, "tax_flag"))},
    )
    assert _run(_OPTIONAL, amount=mixed) == _USD


def test_widening_is_per_coordinate_pinned_nominal_survives() -> None:
    """The converse mix: a per-row currency unit is dropped while a pinned nominal
    binding rides through."""
    mixed = tagged(
        dimension=Dimension.of(PerRow(ColumnRef(_PAYMENTS, "currency"))),
        nominal={"country": Concrete("us")},
    )
    assert _run(_OPTIONAL, amount=mixed) == tagged(nominal={"country": Concrete("us")})
