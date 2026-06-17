"""``check_incremental_worlds``: compile both worlds, check each, diff the findings.

dbt-gated like the rest of the execution harness (it compiles a real project).
The cross-world differencing itself is pinned without dbt in
``tests/check/test_worlds.py`` (``world_varying``); here we pin the orchestration:
both worlds are compiled and checked, tagged by their ``WorldRef``, and a project
with no contracts produces nothing world-varying.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("dbt")

from dblect.adapters import profile_for_adapter
from dblect.execution.incremental import FULL_REFRESH_WORLD, STEADY_STATE_WORLD

_DUCKDB = profile_for_adapter("duckdb")


@pytest.fixture(scope="module")
def incremental_project_dir() -> Path:
    path = Path(__file__).parent.parent / "fixtures" / "incremental"
    if not (path / "dbt_project.yml").exists():
        pytest.skip(f"incremental fixture missing at {path}")
    if shutil.which("dbt") is None:
        pytest.skip("dbt CLI not on PATH")
    return path


def test_both_worlds_are_compiled_and_checked(incremental_project_dir: Path) -> None:
    from dblect.check.incremental import check_incremental_worlds

    result = check_incremental_worlds(incremental_project_dir, _DUCKDB)

    assert result.analyzed_worlds == frozenset({FULL_REFRESH_WORLD, STEADY_STATE_WORLD})
    assert not result.opaque_worlds


def test_project_without_contracts_has_no_cross_world_findings(
    incremental_project_dir: Path,
) -> None:
    from dblect.check.incremental import check_incremental_worlds

    result = check_incremental_worlds(incremental_project_dir, _DUCKDB)

    # The fixture declares no contracts, so the worlds differ in SQL but nothing the
    # checker reports varies across them.
    assert result.cross_world_findings() == {}
