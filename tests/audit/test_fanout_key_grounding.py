# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
"""End to end: a declared ``key()`` quiets a join-fanout false positive.

``dim`` is unique on the composite ``(id, name)`` (a GROUP BY establishes the key),
and ``fact`` joins it on ``id`` alone. On its face the join can multiply rows, so
join-fanout fires. But when the project declares ``dim`` unique on ``id`` alone
(``self.key(self.id)``), that narrower key is itself covered by the join, and the
finding goes quiet.

This is the ``key_facts`` counterpart to ``test_fanout_fd_grounding.py``'s
``determines``-closure test: here the contract adds a key outright rather than a
dependency the reducer folds through an existing one. It pins the wire from a
``key()`` contract through ``resolve_contracts`` and :func:`~dblect.analysis.analyze`
into the join-fanout detector's key coverage. ``analyze`` is the boundary under test:
the gap this guards is in threading ``key_facts`` from the resolved contracts into
``run_audit``, not in the detector once it is given the facts.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.analysis import analyze
from dblect.contracts import ContractSelf, contract
from dblect.manifest import Manifest, Node
from dblect.sql import FindingKind
from dblect.types import ModelContract, isolated_registry, resolve_contracts
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_DUCKDB = profile_for_adapter("duckdb")

_DIM_SQL = "SELECT id, name FROM dim_src GROUP BY id, name"
_FACT_SQL = "SELECT f.id, d.name FROM fact_src AS f JOIN dim AS d ON f.id = d.id"


def _shop_model(name: str, sql: str) -> Node:
    return _node(f"model.shop.{name}", sql)


def _shop_manifest() -> Manifest:
    return _manifest(_shop_model("dim", _DIM_SQL), _shop_model("fact", _FACT_SQL))


def _fanout_models(manifest: Manifest) -> list[str]:
    report = analyze(manifest, _DUCKDB)
    return [
        lf.model_unique_id
        for lf in report.audit.findings
        if lf.finding.kind is FindingKind.JOIN_FANOUT
    ]


def test_fanout_fires_without_a_declared_key() -> None:
    # No key() declared: dim's only key is the GROUP BY's own (id, name), which a
    # join on `id` alone does not cover.
    manifest = _shop_manifest()
    assert resolve_contracts(manifest).key_facts == ()
    assert "model.shop.fact" in _fanout_models(manifest)


def test_declared_key_quiets_the_fanout() -> None:
    with isolated_registry():

        class Dim(ModelContract):
            dbt_model = "dim"

            @contract
            def id_is_a_key(self: ContractSelf) -> object:
                return self.key(self.id)

        manifest = _shop_manifest()
        assert len(resolve_contracts(manifest).key_facts) == 1
        fired = _fanout_models(manifest)
    assert "model.shop.fact" not in fired
