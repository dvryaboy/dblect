"""One model's failure in the check family's join-key scan is a coverage miss for that
model, and the rest of the project is still checked."""

from __future__ import annotations

from dataclasses import replace

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckGraphs, build_check_graphs, enumerate_worlds, run_check
from dblect.lineage.facts.model import BASE_WORLD, WorldRef
from dblect.lineage.graph import ColumnRef
from dblect.lineage.properties.domain_type import DomainTag
from dblect.lineage.property import Annotation
from dblect.manifest import ResourceType
from dblect.model_errors import ModelErrorPolicy, model_error_policy
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_DUCKDB = profile_for_adapter("duckdb")


def _graphs() -> CheckGraphs:
    source = _node(
        "source.shop.raw.t",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(id="INT", poison_id="INT"),
    )
    join = "select a.id from t as a join t as b on a.{k} = b.{k}"
    bad = _node("model.shop.bad", join.format(k="poison_id"), columns=_cols(id="INT"))
    fine = _node("model.shop.fine", join.format(k="id"), columns=_cols(id="INT"))
    return build_check_graphs(_manifest(source, bad, fine), _DUCKDB)


def _poisoning(graphs: CheckGraphs) -> CheckGraphs:
    grounded = graphs.join_key_ground

    def ground(ref: ColumnRef) -> Annotation[DomainTag]:
        if ref.column == "poison_id":
            raise KeyError("boom")
        return grounded(ref)

    return replace(graphs, join_key_ground=ground)


def test_join_key_scan_failure_is_unbuilt_and_names_the_model() -> None:
    graphs = _poisoning(_graphs())
    with model_error_policy(ModelErrorPolicy.SKIP):
        report = run_check(graphs.manifest, _DUCKDB, graphs=graphs)
    reasons = {u.unique_id: u.reason for u in report.unbuilt}
    assert "KeyError" in reasons["model.shop.bad"]
    assert "boom" in reasons["model.shop.bad"]
    assert "model.shop.fine" not in reasons


def test_join_key_scan_failure_propagates_under_the_raise_policy() -> None:
    graphs = _poisoning(_graphs())
    with pytest.raises(KeyError, match="boom"):
        run_check(graphs.manifest, _DUCKDB, graphs=graphs)


def test_world_enumeration_surfaces_join_key_scan_failures_once_per_model() -> None:
    graphs = _poisoning(_graphs())
    other = WorldRef(frozenset({("flag", True)}))
    with model_error_policy(ModelErrorPolicy.SKIP):
        found = enumerate_worlds(graphs, {BASE_WORLD: (), other: ()})
    assert [u.unique_id for u in found.unbuilt()] == ["model.shop.bad"]
    assert all(result.unbuilt for result in found.per_world)
