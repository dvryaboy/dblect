# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# Contract field annotations use the domain-type DSL (``Money.columns(...)``), a value
# expression pyright cannot read as a type; ``test_run_check`` waives the same rule.
"""The fact-level world enumerator: one shared build, per-world findings.

Three contracts pin it. Base-world identity: a single ``BASE_WORLD`` with no compile
facts reproduces ``run_check``'s world-varying findings. Determinism: worlds carrying
identical compile facts produce identical findings, keyed by their world. Cross-world
disagreement: a per-world compile fact that contradicts a downstream contract in one
world and agrees in another yields both results without raising, with the failing
world named.

The per-world ``CompileValue`` tag facts are built by harvesting a correctly-formed
``DomainTag`` through the bridge's own resolution path, then re-stamping the
provenance as a compile value, so the test exercises real tags without reaching into
domain-type internals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from dblect.adapters import profile_for_adapter
from dblect.check import (
    CheckFindingKind,
    TagCompileFact,
    base_world_facts,
    build_check_graphs,
    enumerate_worlds,
    propagate_world,
    run_check,
    world_findings,
)
from dblect.demo import Currency, Money
from dblect.lineage.facts.model import BASE_WORLD, CompileOrigin, CompileValue, Fact, WorldRef
from dblect.lineage.graph import ColumnRef
from dblect.lineage.properties.domain_type import DomainTag
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import Column
from dblect.types import ModelContract, active_registry, isolated_registry, resolve_contracts

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


def _usd_source_fact(manifest: Manifest) -> Fact[DomainTag, ColumnRef]:
    """A correctly-built USD domain-type fact for the source ``amount`` column,
    harvested from a throwaway contract resolved in an isolated registry so it never
    leaks into the base declared facts the enumerator builds from."""
    with isolated_registry() as reg:

        class _Probe(ModelContract):
            dbt_model = "payments"
            amount: MoneyUSD

        return next(iter(resolve_contracts(manifest, registry=reg).tag_facts))


def _eur_source_fact(manifest: Manifest) -> Fact[DomainTag, ColumnRef]:
    """As ``_usd_source_fact``, for EUR."""
    with isolated_registry() as reg:

        class _Probe(ModelContract):
            dbt_model = "payments"
            amount: MoneyEUR

        return next(iter(resolve_contracts(manifest, registry=reg).tag_facts))


def _compile_tag(fact: Fact[DomainTag, ColumnRef], world: WorldRef) -> TagCompileFact:
    return TagCompileFact(
        replace(fact, provenance=CompileValue(origin=CompileOrigin.DBT_VAR, world=world))
    )


def _world_varying(report: object) -> list[CheckFindingKind]:
    assert hasattr(report, "findings")
    keep = {
        CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
        CheckFindingKind.AGGREGATION_NOT_WELL_TYPED,
    }
    return [f.kind for f in report.findings if f.kind in keep]  # type: ignore[attr-defined]


def test_base_world_alone_reproduces_run_checks_world_varying_findings() -> None:
    # A multi-currency source contradicting a single-currency declaration downstream
    # gives run_check a contradiction finding; the base-world enumeration must match.
    class RawPayments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD

    manifest = _passthrough_manifest()
    report = run_check(manifest, _DUCKDB)

    graphs = build_check_graphs(manifest, _DUCKDB)
    result = enumerate_worlds(graphs, {BASE_WORLD: ()})

    assert [r.world for r in result.per_world] == [BASE_WORLD]
    base = result.per_world[0]
    assert [f.kind for f in base.findings] == _world_varying(report)
    # And it equals deriving from the base world directly.
    direct = world_findings(graphs, propagate_world(graphs, base_world_facts(graphs.resolved)))
    assert list(base.findings) == direct


def test_identical_compile_facts_yield_identical_findings() -> None:
    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD

    manifest = _passthrough_manifest()
    usd = _usd_source_fact(manifest)

    graphs = build_check_graphs(manifest, _DUCKDB)
    world_a = WorldRef(frozenset({("currency", "a")}))
    world_b = WorldRef(frozenset({("currency", "b")}))
    result = enumerate_worlds(
        graphs,
        {world_a: (_compile_tag(usd, world_a),), world_b: (_compile_tag(usd, world_b),)},
    )

    by_world = {r.world: r.findings for r in result.per_world}
    assert set(by_world) == {world_a, world_b}
    # Same facts (USD agreeing with the declared USD downstream): same findings.
    assert by_world[world_a] == by_world[world_b]

    coverage = result.coverage()
    assert coverage.worlds_enumerated == 2
    assert coverage.axes_enumerated == ("currency",)


def test_cross_world_disagreement_is_data_not_error() -> None:
    manifest = _passthrough_manifest()
    usd = _usd_source_fact(manifest)
    eur = _eur_source_fact(manifest)

    # Base declared facts: only the downstream single-currency contract.
    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD

    assert active_registry().contracts  # the contract registered into this test's registry

    graphs = build_check_graphs(manifest, _DUCKDB)
    world_usd = WorldRef(frozenset({("currency", "USD")}))
    world_eur = WorldRef(frozenset({("currency", "EUR")}))
    result = enumerate_worlds(
        graphs,
        {
            world_usd: (_compile_tag(usd, world_usd),),
            world_eur: (_compile_tag(eur, world_eur),),
        },
    )

    by_world = {r.world: [f.kind for f in r.findings] for r in result.per_world}
    # USD upstream agrees with the declared USD downstream; EUR contradicts it.
    assert CheckFindingKind.DOMAIN_TYPE_CONTRADICTION not in by_world[world_usd]
    assert CheckFindingKind.DOMAIN_TYPE_CONTRADICTION in by_world[world_eur]

    # The cross-world view names the world the contradiction holds under.
    by_finding = result.by_finding()
    contradiction = next(
        f for f in by_finding if f.kind is CheckFindingKind.DOMAIN_TYPE_CONTRADICTION
    )
    assert by_finding[contradiction] == frozenset({world_eur})


def test_world_varying_flags_findings_holding_in_a_strict_subset() -> None:
    # world_varying is the cross-world signal: a finding present in some enumerated
    # worlds but not all. A finding present in every world is world-invariant and is
    # excluded. This is the differencing the incremental world check reads.
    from dblect.check.findings import CheckFinding
    from dblect.check.worlds import EnumeratedFindings, WorldResult

    w_full = WorldRef(frozenset({("is_incremental", False)}))
    w_steady = WorldRef(frozenset({("is_incremental", True)}))
    shared = CheckFinding(
        kind=CheckFindingKind.CONTRACT_ISSUE, message="shared", model_unique_id="model.p.m"
    )
    steady_only = CheckFinding(
        kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
        message="steady only",
        model_unique_id="model.p.m",
    )

    enumerated = EnumeratedFindings(
        (
            WorldResult(world=w_full, findings=(shared,)),
            WorldResult(world=w_steady, findings=(shared, steady_only)),
        )
    )

    varying = enumerated.world_varying()
    assert shared not in varying
    assert varying == {steady_only: frozenset({w_steady})}
