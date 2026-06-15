# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract's field annotations use the domain-type DSL (``Money.columns(...)``),
# which is a value expression pyright cannot read as a type; ``test_run_check`` waives
# the same rule for the same reason.
"""Staging ``run_check``: one world-invariant build, many per-world propagations.

The single-world behavior stays pinned by ``test_run_check``. Here we pin the seam
the staging introduces: a shared ``CheckGraphs`` build propagates repeatably without
the runs bleeding into one another, which is what lets the world enumerator hold one
build and vary only the facts.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.check import base_world_facts, build_check_graphs, propagate_world
from dblect.demo import Currency, Money
from dblect.lineage.facts.model import BASE_WORLD
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import Column
from dblect.types import ModelContract

_DUCKDB = profile_for_adapter("duckdb")
MoneyUSD = Money.refine(currency=Currency.USD)


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


def _creep_manifest() -> Manifest:
    # A multi-currency source feeding a passthrough, enough lineage that propagation
    # populates annotations rather than staying empty.
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


def test_base_world_facts_mirror_the_resolved_declared_facts() -> None:
    graphs = build_check_graphs(_creep_manifest(), _DUCKDB)
    facts = base_world_facts(graphs.resolved)
    assert facts.world == BASE_WORLD
    assert facts.tag_facts == graphs.resolved.tag_facts
    assert facts.fd_facts == graphs.resolved.fd_facts


def test_one_build_propagates_repeatably() -> None:
    # A multi-currency source contradicting a single-currency declaration downstream,
    # so propagation actually populates annotations rather than staying empty.
    class RawPayments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD

    graphs = build_check_graphs(_creep_manifest(), _DUCKDB)
    facts = base_world_facts(graphs.resolved)

    first = propagate_world(graphs, facts)
    second = propagate_world(graphs, facts)

    assert first.world == BASE_WORLD
    assert first.domain_type  # the shared build really propagated
    assert dict(first.domain_type) == dict(second.domain_type)
