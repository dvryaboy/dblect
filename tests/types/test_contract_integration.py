# pyright: reportInvalidTypeForm=false, reportUnusedClass=false
"""End to end on the jaffle fixture: contract declarations drive the substrate.

The currency-creep story from the demo walkthrough: a PR makes ``raw_payments``
multi-currency and the author records that one truth on the seed's contract.
Downstream, ``stg_payments`` is still declared single-currency USD. The
declared tag and the per-row tag that rides the DAG disagree, and the
reconcile step keeps the declaration tainted provisional, which is the
currency-creep signal a reporter renders. The taint rides on to ``orders``,
a model nobody declared anything about.

The seed's columns are documented in the manifest here (the multi-currency PR
adds the ``currency`` column to ``schema.yml``) so the staging model's
``select *`` expands and lineage reaches the seed.
"""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from dblect.demo import Currency, Money
from dblect.lineage.builder import build_manifest_graph
from dblect.lineage.facts.grounding import collect
from dblect.lineage.facts.model import Annotation
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    Concrete,
    Dimension,
    DomainTag,
    PerRow,
    domain_type_grounding,
    domain_type_property,
    tagged,
)
from dblect.lineage.property import propagate
from dblect.manifest import Manifest
from dblect.manifest.parse import Column
from dblect.types import ModelContract, contract_tag_discoverer, resolve_contracts

MoneyUSD = Money.refine(currency=Currency.USD)

_SEED = SourceRef(SourceKind.SEED, "seed.jaffle_shop.raw_payments")
_STG = SourceRef(SourceKind.MODEL, "model.jaffle_shop.stg_payments")
_ORDERS = SourceRef(SourceKind.MODEL, "model.jaffle_shop.orders")
_USD = tagged(dimension=Dimension.of(Concrete("usd")))


def _cols(**types: str) -> Mapping[str, Column]:
    return {n: Column(name=n, data_type=t, description=None) for n, t in types.items()}


_DOCUMENTED: Mapping[str, Mapping[str, Column]] = {
    "seed.jaffle_shop.raw_payments": _cols(
        id="INT", order_id="INT", payment_method="VARCHAR", amount="INT", currency="VARCHAR"
    ),
    "model.jaffle_shop.stg_payments": _cols(
        payment_id="INT", order_id="INT", payment_method="VARCHAR", amount="DECIMAL"
    ),
    "model.jaffle_shop.stg_orders": _cols(
        order_id="INT", customer_id="INT", order_date="DATE", status="VARCHAR"
    ),
    "model.jaffle_shop.stg_customers": _cols(
        customer_id="INT", first_name="VARCHAR", last_name="VARCHAR"
    ),
}


def _documented_jaffle(path: Path) -> Manifest:
    manifest = Manifest.from_file(path)
    nodes = {
        uid: replace(node, columns=_DOCUMENTED[uid]) if uid in _DOCUMENTED else node
        for uid, node in manifest.nodes.items()
    }
    return Manifest(
        schema_version=manifest.schema_version,
        adapter_type=manifest.adapter_type,
        nodes=nodes,
    )


def _propagate(manifest: Manifest) -> Mapping[ColumnRef, Annotation[DomainTag]]:
    resolved = resolve_contracts(manifest)
    assert resolved.issues == ()
    build = build_manifest_graph(manifest)
    assert build.issues == ()
    name_to_source: Mapping[str, SourceRef] = {}
    facts = collect(manifest, (contract_tag_discoverer(),), name_to_source=name_to_source)
    return propagate(build.graph, domain_type_property(domain_type_grounding(facts)))


def test_currency_creep_reaches_models_nobody_declared(jaffle_manifest_path: Path) -> None:
    manifest = _documented_jaffle(jaffle_manifest_path)

    class RawPayments(ModelContract):
        dbt_model = "raw_payments"
        amount: Money.columns(amount="amount", currency="currency")

    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD

    resolved = resolve_contracts(manifest)
    per_row = tagged(dimension=Dimension.of(PerRow(ColumnRef(_SEED, "currency"))))
    assert {f.scope: f.value for f in resolved.tag_facts} == {
        ColumnRef(_SEED, "amount"): per_row,
        ColumnRef(_STG, "amount"): _USD,
    }

    anns = _propagate(manifest)

    creep = anns[ColumnRef(_STG, "amount")]
    assert creep.value == _USD  # the declaration is kept...
    assert creep.provisional  # ...tainted: the inferred per-row Money contradicts it

    downstream = anns[ColumnRef(_ORDERS, "amount")]
    assert downstream.value == _USD
    assert downstream.provisional  # the blast radius reaches an undeclared model


def test_consistent_single_currency_declarations_stay_quiet(
    jaffle_manifest_path: Path,
) -> None:
    manifest = _documented_jaffle(jaffle_manifest_path)

    class RawPayments(ModelContract):
        dbt_model = "raw_payments"
        amount: MoneyUSD

    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD

    anns = _propagate(manifest)
    for ref in (ColumnRef(_STG, "amount"), ColumnRef(_ORDERS, "amount")):
        ann = anns[ref]
        assert ann.value == _USD
        assert not ann.provisional
