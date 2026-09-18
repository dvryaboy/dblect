# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
"""End to end: a key read from the propagated uniqueness property quiets a
join-fanout false positive through the FD property's uniqueness edge.

``dim_src`` is declared unique on ``id``. ``dim_stg`` passes it through
unchanged, so its own key stays ``id`` and its FD set gains ``id -> name`` (a
relation unique on ``id`` determines every column selected alongside it).
``dim`` then groups by ``(id, name)``: a GROUP BY takes on a new grain, so
``dim``'s own structural key is the composite pair, not ``id`` alone, but the
``id -> name`` dependency carries through the grouping (it holds within the
group key already). ``fact`` joins ``dim`` on ``id`` alone. On its face the join
covers neither known key, so join-fanout fires; with ``id -> name`` in ``dim``'s
FD set the join's ``id`` closes up to the composite key, and the finding goes
quiet. Without the declared key the same join still fires, the genuine true
positive.

This pins the production wire from ``functional_dependency_property``'s
``uniqueness`` edge through ``relation_uniqueness``/``fd_annotations_by_name``
into ``run_audit``'s join-fanout detector. It complements
``test_fanout_fd_grounding.py``, whose FD comes from a declared ``determines``
contract; here it comes from a declared key alone.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.audit import run_audit
from dblect.manifest import DbtTestMetadata, Manifest, ResourceType
from dblect.sql import FindingKind
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source

_DUCKDB = profile_for_adapter("duckdb")

_DIM_STG_SQL = "SELECT id, name FROM dim_src"
_DIM_SQL = "SELECT id, name FROM dim_stg GROUP BY id, name"
_FACT_SQL = "SELECT f.x, d.name FROM fact_src AS f JOIN dim AS d ON f.id = d.id"


def _shop_manifest(*, declare_key: bool) -> Manifest:
    dim_src = _source("source.shop.raw.dim_src")
    dim_stg = _node("model.shop.dim_stg", _DIM_STG_SQL)
    dim = _node("model.shop.dim", _DIM_SQL)
    fact = _node("model.shop.fact", _FACT_SQL)
    if not declare_key:
        return _manifest(dim_src, dim_stg, dim, fact)
    unique_test = _node(
        "test.shop.unique_dim_src_id",
        kind=ResourceType.OTHER,
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": "id"}),
        attached_node=dim_src.unique_id,
    )
    return _manifest(dim_src, unique_test, dim_stg, dim, fact)


def _fanout_kinds(manifest: Manifest) -> list[str]:
    report = run_audit(manifest, _DUCKDB)
    return [
        lf.model_unique_id for lf in report.findings if lf.finding.kind is FindingKind.JOIN_FANOUT
    ]


def test_fanout_fires_without_the_declared_key() -> None:
    assert "model.shop.fact" in _fanout_kinds(_shop_manifest(declare_key=False))


def test_key_derived_fd_quiets_the_fanout() -> None:
    assert "model.shop.fact" not in _fanout_kinds(_shop_manifest(declare_key=True))
