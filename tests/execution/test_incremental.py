"""The incremental world-compiler produces both compilations of a project from
``dbt compile`` alone, data-free.

These tests drive the dbt CLI through the shared ``dbt_cli`` fixture, so they run
under ``uv run`` and skip where dbt is absent (see ``tests/conftest.py``). The
mechanism is pinned at the boundary: a project directory in, two per-world
``Manifest``s out, with the ``is_incremental()`` branch present in the steady-state
world and absent in the full-refresh world.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dblect.manifest import Manifest, Node


@pytest.fixture(scope="module")
def incremental_project_dir() -> Path:
    path = Path(__file__).parent.parent / "fixtures" / "incremental"
    if not (path / "dbt_project.yml").exists():
        pytest.skip(f"incremental fixture missing at {path}")
    return path


def _model(manifest: Manifest, name: str) -> Node:
    matches = [n for n in manifest.models.values() if n.name == name]
    assert len(matches) == 1, f"expected one model named {name}, got {matches}"
    return matches[0]


def test_both_worlds_compile(incremental_project_dir: Path, dbt_cli: str) -> None:
    from dblect.execution.incremental import compile_incremental_worlds

    result = compile_incremental_worlds(incremental_project_dir, dbt_executable=dbt_cli)
    assert result.full_refresh.ok, result.full_refresh.error
    assert result.steady_state.ok, result.steady_state.error


def test_worlds_carry_the_incremental_assignment(
    incremental_project_dir: Path, dbt_cli: str
) -> None:
    from dblect.execution.incremental import (
        FULL_REFRESH_WORLD,
        STEADY_STATE_WORLD,
        compile_incremental_worlds,
    )

    result = compile_incremental_worlds(incremental_project_dir, dbt_executable=dbt_cli)
    assert result.full_refresh.world == FULL_REFRESH_WORLD
    assert result.steady_state.world == STEADY_STATE_WORLD
    # The two worlds are distinct assignments of the one run-mode axis.
    assert FULL_REFRESH_WORLD != STEADY_STATE_WORLD


def test_watermark_branch_present_only_in_steady_state(
    incremental_project_dir: Path, dbt_cli: str
) -> None:
    from dblect.execution.incremental import compile_incremental_worlds

    result = compile_incremental_worlds(incremental_project_dir, dbt_executable=dbt_cli)
    assert result.full_refresh.manifest is not None
    assert result.steady_state.manifest is not None

    full = _model(result.full_refresh.manifest, "inc_watermark").compiled_code or ""
    steady = _model(result.steady_state.manifest, "inc_watermark").compiled_code or ""

    assert "max(event_time)" not in full.lower()
    assert "max(event_time)" in steady.lower()


def test_structure_adding_branch_only_in_steady_state(
    incremental_project_dir: Path, dbt_cli: str
) -> None:
    from dblect.execution.incremental import compile_incremental_worlds

    result = compile_incremental_worlds(incremental_project_dir, dbt_executable=dbt_cli)
    assert result.full_refresh.manifest is not None
    assert result.steady_state.manifest is not None

    full = _model(result.full_refresh.manifest, "inc_stateful")
    steady = _model(result.steady_state.manifest, "inc_stateful")
    full_sql = (full.compiled_code or "").lower()
    steady_sql = (steady.compiled_code or "").lower()

    # The join, the extra column, and the new dependency appear only in steady-state.
    assert "left join" not in full_sql
    assert "left join" in steady_sql
    assert "last_seen" not in full_sql
    assert "last_seen" in steady_sql

    full_deps = {uid for uid in full.depends_on if uid.endswith(".state")}
    steady_deps = {uid for uid in steady.depends_on if uid.endswith(".state")}
    assert not full_deps
    assert steady_deps
