# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
"""End to end: an authored ``determines`` consolidates a join-on-nullable-key finding.

``dim`` exposes ``store_id, region_id, country_id`` from the optional side of a LEFT JOIN, so
all three are nullable upstream. ``fact`` denormalizes the hierarchy, joining ``dim`` on all
three columns. On its face that is three co-equal nullable keys. When the project declares
``store_id determines region_id`` and ``region_id determines country_id``, the join is really
keyed on ``store_id`` alone, the other equalities functionally redundant, so the finding
consolidates onto the declared root and points at dropping the redundant conditions. It never
goes silent: the columns are still nullable, so the null-non-match risk is real.

This pins the wire from a ``determines`` contract through ``resolve_contracts`` and the shared
``fd_annotations_by_name`` propagation in ``run_audit`` into the join-on-nullable-key detector.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.audit import run_audit
from dblect.contracts import ContractSelf, contract
from dblect.manifest import Manifest, Node, ResourceType
from dblect.sql import FindingKind
from dblect.types import ModelContract, isolated_registry, resolve_contracts

_DUCKDB = profile_for_adapter("duckdb")

# The hierarchy columns are drawn from the optional side of a LEFT JOIN, so they are nullable
# in ``dim``'s output regardless of what the sources hold.
_DIM_SQL = (
    "SELECT s.store_id, s.region_id, s.country_id "
    "FROM anchor a LEFT JOIN store s ON a.id = s.store_id"
)
_FACT_SQL = (
    "SELECT f.v FROM fact_src AS f JOIN dim AS d "
    "ON f.store_id = d.store_id AND f.region_id = d.region_id AND f.country_id = d.country_id"
)


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


def _join_message(manifest: Manifest, *, with_facts: bool) -> str:
    fd_facts = resolve_contracts(manifest).fd_facts if with_facts else ()
    report = run_audit(manifest, _DUCKDB, fd_facts=fd_facts)
    messages = [
        lf.finding.message
        for lf in report.findings
        if lf.finding.kind is FindingKind.JOIN_ON_NULLABLE_KEY
        and lf.model_unique_id == "model.shop.fact"
    ]
    assert len(messages) == 1
    return messages[0]


def test_without_the_dependency_every_hierarchy_column_is_listed() -> None:
    with isolated_registry():
        manifest = _manifest()
        assert resolve_contracts(manifest).fd_facts == ()
        msg = _join_message(manifest, with_facts=True)
    assert all(col in msg for col in ("store_id", "region_id", "country_id"))
    assert "redundant" not in msg.lower()


def test_declared_dependency_consolidates_onto_the_root_key() -> None:
    with isolated_registry():

        class Dim(ModelContract):
            dbt_model = "dim"

            @contract
            def store_determines_region(self: ContractSelf) -> object:
                return self.store_id.determines(self.region_id)

            @contract
            def region_determines_country(self: ContractSelf) -> object:
                return self.region_id.determines(self.country_id)

        manifest = _manifest()
        assert len(resolve_contracts(manifest).fd_facts) == 2
        msg = _join_message(manifest, with_facts=True)
    assert "keys on store_id, which is nullable upstream" in msg
    assert "functionally determined by the declared key store_id" in msg
    assert "redundant" in msg.lower()
