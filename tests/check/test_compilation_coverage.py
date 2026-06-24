"""Stale or absent ``compiled_code`` is a coverage miss, never analysed as empty.

A node whose compiled SQL does not faithfully represent the model (empty or stale
while the template is non-trivial, or flagged not-compiled by the manifest) must
surface as a coverage miss rather than be read as a clean, empty model. The
lineage builds skip it and record the reason; the audit walker skips it; neither
analyses an empty body as if it were the model.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.audit import run_audit
from dblect.check import run_check
from dblect.lineage.builder import build_manifest_graph
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import Column

_DUCKDB = profile_for_adapter("duckdb")


def _cols(**types: str) -> Mapping[str, Column]:
    return {n: Column(name=n, data_type=t, description=None) for n, t in types.items()}


def _model(
    uid: str,
    *,
    raw: str | None,
    compiled: str | None,
    compiled_flag: bool | None,
    columns: Mapping[str, Column],
) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=tuple(uid.split(".")[1:]),
        package_name="shop",
        schema="analytics",
        raw_code=raw,
        compiled_code=compiled,
        original_file_path=f"models/{uid.split('.')[-1]}.sql",
        columns=columns,
        compiled_flag=compiled_flag,
    )


def _manifest(*nodes: Node) -> Manifest:
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def _stale() -> Node:
    return _model(
        "model.shop.stale",
        raw="select max(updated_at) from events",
        compiled="",
        compiled_flag=True,
        columns=_cols(updated_at="TIMESTAMP"),
    )


def test_stale_compiled_code_is_a_build_miss_not_an_empty_model() -> None:
    build = build_manifest_graph(_manifest(_stale()))
    [issue] = [i for i in build.issues if i.model_unique_id == "model.shop.stale"]
    assert "stale" in issue.message or "did not reach the warehouse" in issue.message
    # Nothing was emitted: the model was not analysed as if empty.
    self_ref = SourceRef(kind=SourceKind.MODEL, unique_id="model.shop.stale")
    assert not any(ref.source == self_ref for ref in build.graph.edges)


def test_run_check_surfaces_a_stale_node_as_unbuilt() -> None:
    report = run_check(_manifest(_stale()), _DUCKDB)
    assert any(m.unique_id == "model.shop.stale" for m in report.unbuilt)
    # An unbuilt model is excluded from the analysed count, so a clean finding list
    # does not overstate coverage.
    assert report.models_analyzed == report.models_propagated - len(report.unbuilt)


def test_audit_skips_a_stale_node_with_the_compilation_reason() -> None:
    report = run_audit(_manifest(_stale()), _DUCKDB)
    skipped = [s for s in report.skipped if s.unique_id == "model.shop.stale"]
    assert skipped, "a stale-compiled node should be skipped, not scanned"
    assert "stale" in skipped[0].reason or "warehouse" in skipped[0].reason
    assert report.models_scanned == 0


def test_a_genuinely_compiled_model_still_builds() -> None:
    # The miss check must not block a faithfully compiled model.
    ok = _model(
        "model.shop.ok",
        raw="select id from upstream",
        compiled="SELECT id FROM upstream",
        compiled_flag=True,
        columns=_cols(id="INT"),
    )
    build = build_manifest_graph(_manifest(ok))
    assert build.issues == ()
    self_ref = SourceRef(kind=SourceKind.MODEL, unique_id="model.shop.ok")
    assert ColumnRef(source=self_ref, column="id") in build.graph.edges
