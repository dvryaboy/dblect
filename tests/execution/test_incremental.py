"""The incremental world-compiler produces both compilations of a project from
``dbt compile`` alone, data-free.

These tests drive the dbt CLI through the shared ``dbt_cli`` fixture, so they run
under ``uv run`` and skip where dbt is absent (see ``tests/conftest.py``). The two
compiles are run once for the module and shared, since they are the slow part.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dblect.execution.incremental import (
    FULL_REFRESH_WORLD,
    STEADY_STATE_WORLD,
    IncrementalWorldCompilation,
    compile_incremental_worlds,
)
from dblect.manifest import Manifest, Node


@pytest.fixture(scope="module")
def incremental_project_dir() -> Path:
    path = Path(__file__).parent.parent / "fixtures" / "incremental"
    if not (path / "dbt_project.yml").exists():
        pytest.skip(f"incremental fixture missing at {path}")
    return path


@pytest.fixture(scope="module")
def compiled(incremental_project_dir: Path, dbt_cli: str) -> IncrementalWorldCompilation:
    return compile_incremental_worlds(incremental_project_dir, dbt_executable=dbt_cli)


def _model(manifest: Manifest | None, name: str) -> Node:
    assert manifest is not None
    matches = [n for n in manifest.models.values() if n.name == name]
    assert len(matches) == 1, f"expected one model named {name}, got {matches}"
    return matches[0]


def test_both_worlds_compile(compiled: IncrementalWorldCompilation) -> None:
    assert compiled.full_refresh.ok, compiled.full_refresh.error
    assert compiled.steady_state.ok, compiled.steady_state.error


def test_worlds_carry_the_incremental_assignment(compiled: IncrementalWorldCompilation) -> None:
    assert compiled.full_refresh.world == FULL_REFRESH_WORLD
    assert compiled.steady_state.world == STEADY_STATE_WORLD
    # The two worlds are distinct assignments of the one run-mode axis.
    assert FULL_REFRESH_WORLD != STEADY_STATE_WORLD


def test_watermark_branch_present_only_in_steady_state(
    compiled: IncrementalWorldCompilation,
) -> None:
    full = _model(compiled.full_refresh.manifest, "inc_watermark").compiled_code or ""
    steady = _model(compiled.steady_state.manifest, "inc_watermark").compiled_code or ""

    assert "max(event_time)" not in full.lower()
    assert "max(event_time)" in steady.lower()


def test_structure_adding_branch_only_in_steady_state(
    compiled: IncrementalWorldCompilation,
) -> None:
    full = _model(compiled.full_refresh.manifest, "inc_stateful")
    steady = _model(compiled.steady_state.manifest, "inc_stateful")
    full_sql = (full.compiled_code or "").lower()
    steady_sql = (steady.compiled_code or "").lower()

    # The join, the extra column, and the new dependency appear only in steady-state.
    assert "left join" not in full_sql
    assert "left join" in steady_sql
    assert "last_seen" not in full_sql
    assert "last_seen" in steady_sql

    assert not {uid for uid in full.depends_on if uid.endswith(".state")}
    assert {uid for uid in steady.depends_on if uid.endswith(".state")}
