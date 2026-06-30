# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
"""End to end: an authored ``determines`` quiets a join-fanout false positive.

``dim`` is unique on the composite ``(a, b, c)`` (a GROUP BY establishes the key), and
``fact`` joins it on ``a`` alone. On its face the join can multiply rows, so join-fanout
fires. But when the project declares ``a determines b`` and ``a determines c``, the join's
column functionally determines the whole key, so the join cannot fan out. The contract's
``determines`` facts are resolved and threaded into the structural audit, and the finding
goes quiet. Without the contract the same join still fires, the genuine true positive.

This pins the wire from a ``determines`` contract through ``resolve_contracts`` and
``run_audit`` into the join-fanout detector's closure-based key coverage.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.audit import run_audit
from dblect.contracts import ContractSelf, contract
from dblect.manifest import Manifest, Node, ResourceType
from dblect.sql import FindingKind
from dblect.types import ModelContract, resolve_contracts

_DUCKDB = profile_for_adapter("duckdb")

_DIM_SQL = "SELECT a, b, c FROM dim_src GROUP BY a, b, c"
_FACT_SQL = "SELECT f.a, d.b FROM fact_src AS f JOIN dim AS d ON f.a = d.a"


def _manifest() -> Manifest:
    def model(name: str, sql: str) -> Node:
        return Node(
            unique_id=f"model.shop.{name}",
            name=name,
            resource_type=ResourceType.MODEL,
            fqn=("shop", name),
            package_name="shop",
            schema="analytics",
            raw_code=None,
            compiled_code=sql,
            original_file_path=None,
            columns={},
        )

    nodes = (model("dim", _DIM_SQL), model("fact", _FACT_SQL))
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def _fanout_kinds(manifest: Manifest, *, with_facts: bool) -> list[str]:
    fd_facts = resolve_contracts(manifest).fd_facts if with_facts else ()
    report = run_audit(manifest, _DUCKDB, fd_facts=fd_facts)
    return [
        lf.model_unique_id for lf in report.findings if lf.finding.kind is FindingKind.JOIN_FANOUT
    ]


def test_fanout_fires_without_the_dependency() -> None:
    # No FD declared: a join on `a` does not cover the (a, b, c) key, so the fanout fires.
    manifest = _manifest()
    assert resolve_contracts(manifest).fd_facts == ()
    assert "model.shop.fact" in _fanout_kinds(manifest, with_facts=True)


def test_declared_dependency_quiets_the_fanout() -> None:
    class Dim(ModelContract):
        dbt_model = "dim"

        @contract
        def a_determines_b(self: ContractSelf) -> object:
            return self.a.determines(self.b)

        @contract
        def a_determines_c(self: ContractSelf) -> object:
            return self.a.determines(self.c)

    manifest = _manifest()
    assert len(resolve_contracts(manifest).fd_facts) == 2
    assert "model.shop.fact" not in _fanout_kinds(manifest, with_facts=True)
