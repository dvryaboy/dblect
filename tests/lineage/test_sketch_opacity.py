"""Opaque sketch aggregates do not propagate a domain tag (issue #90).

Approximate-distinct sketches (BigQuery ``HLL_COUNT.*`` and ``APPROX_COUNT_DISTINCT``)
store an opaque value with no row-level identity, so a domain-type tag on the input
must not flow through them: the result is the no-claim top (``NAKED``), never the
pre-sketch column's tag. These pin that the sketch functions degrade to top rather
than inherit the input's facts.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Annotation, Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    NAKED,
    Concrete,
    Dimension,
    DomainTag,
    domain_type_grounding,
    domain_type_property,
    tagged,
)

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.events")
_MODEL = SourceRef(SourceKind.MODEL, "model.shop.m")
_USD = tagged(dimension=Dimension.of(Concrete("usd")))


def _facts(**by_column: DomainTag) -> Mapping[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]]:
    out: dict[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]] = {}
    for column, value in by_column.items():
        ref = ColumnRef(_SRC, column)
        out[ref] = (
            Fact(scope=ref, value=value, provenance=Declared(DeclaredSource.USER_ASSERTED)),
        )
    return out


def _run(
    sql: str, facts: Mapping[ColumnRef, tuple[Fact[DomainTag, ColumnRef], ...]], *, out: str
) -> Annotation[DomainTag]:
    graph = build_model_graph(
        model_uid=_MODEL.unique_id,
        sql=sql,
        name_to_source={"events": _SRC},
        schema={"events": {"amount": "DECIMAL", "sketch": "BYTES"}},
        dialect="bigquery",
    )
    anns = propagate(graph, domain_type_property(domain_type_grounding(facts)))
    return anns[ColumnRef(_MODEL, out)]


def test_approx_count_distinct_does_not_carry_the_input_tag() -> None:
    ann = _run(
        "SELECT APPROX_COUNT_DISTINCT(e.amount) AS n FROM events e", _facts(amount=_USD), out="n"
    )
    assert ann.value == NAKED


def test_hll_init_does_not_carry_the_input_tag() -> None:
    ann = _run(
        "SELECT HLL_COUNT.INIT(e.amount) AS sketch FROM events e", _facts(amount=_USD), out="sketch"
    )
    assert ann.value == NAKED


def test_hll_extract_does_not_carry_the_input_tag() -> None:
    # Tag the sketch column so this pins that EXTRACT drops a tag on its input,
    # not merely that an untagged input stays untagged.
    ann = _run(
        "SELECT HLL_COUNT.EXTRACT(e.sketch) AS n FROM events e", _facts(sketch=_USD), out="n"
    )
    assert ann.value == NAKED


def test_plain_rename_still_carries_the_tag() -> None:
    # Guard against over-broadly clearing tags: an ordinary projection is unaffected.
    ann = _run("SELECT e.amount AS total FROM events e", _facts(amount=_USD), out="total")
    assert ann.value == _USD
