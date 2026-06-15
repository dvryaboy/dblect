# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# Contract field annotations use the domain-type DSL (``Money.refine(...)``), a value
# expression pyright cannot read as a type; ``test_run_check`` waives the same rule.
"""End to end: a hand-declared ``DomainFlag`` yields a cross-world finding.

This is the first var-driven check the world plumbing was built for, exercised ahead
of var-inference by declaring the flag and its responsive scope directly. A flag that
sets an upstream column's currency drives a downstream single-currency contract to
pass in the world that agrees and fail in the world that does not.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.check import (
    CheckFindingKind,
    DomainFlag,
    build_check_graphs,
    check_worlds,
    flag_worlds,
)
from dblect.demo import Currency, Money
from dblect.lineage.facts.model import BASE_WORLD, WorldRef
from dblect.lineage.graph import ColumnRef
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import Column
from dblect.types import ModelContract, isolated_registry, resolve_contracts

_DUCKDB = profile_for_adapter("duckdb")
MoneyUSD = Money.refine(currency=Currency.USD)
MoneyEUR = Money.refine(currency=Currency.EUR)


def _cols(**types: str) -> Mapping[str, Column]:
    return {n: Column(name=n, data_type=t, description=None) for n, t in types.items()}


def _node(uid: str, *, kind: ResourceType, sql: str | None, columns: Mapping[str, Column]) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=kind,
        fqn=tuple(uid.split(".")[1:]),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=f"models/{uid.split('.')[-1]}.sql",
        columns=columns,
    )


def _passthrough_manifest() -> Manifest:
    nodes = (
        _node(
            "source.shop.raw.payments",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount="DECIMAL", currency="VARCHAR"),
        ),
        _node(
            "model.shop.stg_payments",
            kind=ResourceType.MODEL,
            sql="SELECT amount FROM payments",
            columns=_cols(amount="DECIMAL"),
        ),
    )
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def _source_amount_scope(manifest: Manifest) -> ColumnRef:
    """The exact ColumnRef the graph keys the source ``amount`` column on, harvested
    through the bridge so the flag's responsive scope matches what propagation walks.
    Resolved in an isolated registry so the probe contract does not leak."""
    with isolated_registry() as reg:

        class _Probe(ModelContract):
            dbt_model = "payments"
            amount: MoneyUSD

        return next(iter(resolve_contracts(manifest, registry=reg).tag_facts)).scope


def test_flag_worlds_with_no_flags_is_the_base_world() -> None:
    assert flag_worlds([]) == {BASE_WORLD: ()}


def test_a_flag_yields_a_cross_world_finding() -> None:
    manifest = _passthrough_manifest()
    scope = _source_amount_scope(manifest)

    # The only declared fact: the downstream contract pins the currency to USD.
    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD

    graphs = build_check_graphs(manifest, _DUCKDB)
    flag = DomainFlag(
        name="currency",
        affects={"USD": MoneyUSD, "EUR": MoneyEUR},
        scopes=(scope,),
    )
    result = check_worlds(graphs, [flag])

    world_usd = WorldRef(frozenset({("currency", "USD")}))
    world_eur = WorldRef(frozenset({("currency", "EUR")}))
    by_world = {r.world: [f.kind for f in r.findings] for r in result.per_world}
    assert set(by_world) == {world_usd, world_eur}
    # USD upstream agrees with the declared USD downstream; EUR contradicts it.
    assert CheckFindingKind.DOMAIN_TYPE_CONTRADICTION not in by_world[world_usd]
    assert CheckFindingKind.DOMAIN_TYPE_CONTRADICTION in by_world[world_eur]

    # The cross-world view names the failing world, and coverage reports the sweep.
    by_finding = result.by_finding()
    contradiction = next(
        f for f in by_finding if f.kind is CheckFindingKind.DOMAIN_TYPE_CONTRADICTION
    )
    assert by_finding[contradiction] == frozenset({world_eur})

    coverage = result.coverage()
    assert coverage.worlds_enumerated == 2
    assert coverage.axes_enumerated == ("currency",)
