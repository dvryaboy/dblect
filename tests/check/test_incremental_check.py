"""``check_incremental_worlds``: compile both worlds, check each, diff the findings.

dbt-gated like the rest of the execution harness (it compiles a real project). The
cross-world differencing itself is pinned without dbt in ``test_cross_world.py``;
here we pin the end-to-end behavior against a real ``dbt compile``.

The fixture carries the classic incremental hazard. ``inc_stateful`` claims grain
``id`` (``unique_key='id'``) and, only in its steady-state branch, enriches each
row by joining the ``state`` history log on ``id`` alone. ``state`` has its own
surrogate key ``state_id`` and several rows per ``id``, so that join can multiply
rows: ``id`` is unique in the full-refresh build and fanned out in the steady-state
build. The join-fan-out detector fires in steady-state alone, which is exactly the
cross-world signal this stream exists to surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dblect.adapters import profile_for_adapter
from dblect.audit import LocatedFinding
from dblect.check.incremental import IncrementalWorldCheck, check_incremental_worlds
from dblect.execution.incremental import FULL_REFRESH_WORLD, STEADY_STATE_WORLD
from dblect.sql import FindingKind
from dblect.types import isolated_registry

_DUCKDB = profile_for_adapter("duckdb")


@pytest.fixture(scope="module")
def incremental_project_dir() -> Path:
    path = Path(__file__).parent.parent / "fixtures" / "incremental"
    if not (path / "dbt_project.yml").exists():
        pytest.skip(f"incremental fixture missing at {path}")
    return path


@pytest.fixture(scope="module")
def result(incremental_project_dir: Path, dbt_cli: str) -> IncrementalWorldCheck:
    # No contracts: the signal comes from the dbt-test-declared keys and the SQL the
    # two worlds compile to, not from a declaration layered on top. An isolated
    # registry keeps contracts from other test modules out.
    with isolated_registry() as registry:
        return check_incremental_worlds(
            incremental_project_dir, _DUCKDB, registry=registry, dbt_executable=dbt_cli
        )


def test_both_worlds_are_compiled_and_checked(result: IncrementalWorldCheck) -> None:
    assert result.analyzed_worlds == frozenset({FULL_REFRESH_WORLD, STEADY_STATE_WORLD})
    assert not result.opaque_worlds


def test_steady_state_fan_out_is_the_cross_world_finding(result: IncrementalWorldCheck) -> None:
    (varying,) = result.cross_world_findings()
    assert varying.worlds == frozenset({STEADY_STATE_WORLD})

    representative = varying.representative
    assert isinstance(representative, LocatedFinding)
    assert representative.finding.kind is FindingKind.JOIN_FANOUT
    assert representative.model_unique_id == "model.incremental_fixture.inc_stateful"
