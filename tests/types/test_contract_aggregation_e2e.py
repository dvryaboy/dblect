# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance, so annotating it that way trips pyright's "self is a supertype of its
# class" rule; the proxy usage itself stays fully checked. Typed ``self`` access in
# authored contracts is the deferred generated-stubs concern.
"""End to end: an authored ``determines`` discharges a grouped sum.

This is the headline the contract surface promises. ``payments.amount`` is a
``Money`` whose currency rides as a per-row companion, and a downstream mart sums
it grouped by ``country``. Summing a per-row currency is not well typed on its
own, so the aggregate clears to the naked tag, the mixed-currency-sum finding. The
author states the one truth that makes it sound, ``country -> currency``, as a
``@contract`` method, and the framework carries that dependency to the
aggregation and keeps the tag.

The facts here come only from declarations: the domain tag and the dependency are
both authored, resolved through the bridge, and propagated by the substrate. No
data is read.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.contracts import ContractSelf, contract
from dblect.demo import Money
from dblect.lineage.builder import build_manifest_graph, build_relation_graph
from dblect.lineage.facts.grounding import collect
from dblect.lineage.facts.model import Annotation
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    NAKED,
    Dimension,
    DomainTag,
    PerRow,
    domain_type_grounding,
    domain_type_property,
    tagged,
)
from dblect.lineage.properties.functional_dependency import (
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import Column
from dblect.types import (
    ModelContract,
    contract_fd_discoverer,
    contract_tag_discoverer,
    resolve_contracts,
)

_PAYMENTS = SourceRef(SourceKind.SOURCE, "source.shop.raw.payments")
_MART = SourceRef(SourceKind.MODEL, "model.shop.revenue_by_country")
_TOTAL = ColumnRef(_MART, "total")
_PER_ROW = tagged(dimension=Dimension.of(PerRow(ColumnRef(_PAYMENTS, "currency"))))

_SQL = "SELECT country, SUM(amount) AS total FROM payments GROUP BY country"


def _cols(*names: str) -> Mapping[str, Column]:
    return {n: Column(name=n, data_type="VARCHAR", description=None) for n in names}


def _manifest() -> Manifest:
    payments = Node(
        unique_id=_PAYMENTS.unique_id,
        name="payments",
        resource_type=ResourceType.SOURCE,
        fqn=("shop", "raw", "payments"),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns=_cols("amount", "currency", "country"),
    )
    mart = Node(
        unique_id=_MART.unique_id,
        name="revenue_by_country",
        resource_type=ResourceType.MODEL,
        fqn=("shop", "revenue_by_country"),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=_SQL,
        original_file_path=None,
        columns={},
    )
    return Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in (payments, mart)},
    )


def _aggregate_tag(manifest: Manifest) -> Annotation[DomainTag]:
    """Propagate the FD property over the relation graph, then domain type over the
    column graph reading it, and return the aggregate output's tag. Both properties
    ground only from the registered contracts."""
    assert resolve_contracts(manifest).issues == ()
    name_to_source: Mapping[str, SourceRef] = {}

    fd_facts = collect(manifest, (contract_fd_discoverer(),), name_to_source=name_to_source)
    fd_prop = functional_dependency_property(functional_dependency_grounding(fd_facts))
    store = AnnotationStore()
    relation_graph = build_relation_graph(manifest).graph
    for scope, ann in propagate(relation_graph, fd_prop).items():
        store.record(fd_prop.name, scope, ann)

    tag_facts = collect(manifest, (contract_tag_discoverer(),), name_to_source=name_to_source)
    dt_prop = domain_type_property(domain_type_grounding(tag_facts), fd=fd_prop.ref)
    ctx = PropertyRegistry((fd_prop, dt_prop)).dep_context(store)

    column_graph = build_manifest_graph(manifest).graph
    return propagate(column_graph, dt_prop, dep_context=ctx)[_TOTAL]


def test_authored_dependency_discharges_the_grouped_sum() -> None:
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

        @contract
        def country_sets_currency(self: ContractSelf) -> object:
            return self.country.determines(self.currency)

    ann = _aggregate_tag(_manifest())
    assert ann.value == _PER_ROW  # the dependency kept the currency tag through the sum


def test_without_the_dependency_the_sum_clears() -> None:
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    ann = _aggregate_tag(_manifest())
    assert ann.value == NAKED  # mixed-currency sum: not well typed, the finding fires
