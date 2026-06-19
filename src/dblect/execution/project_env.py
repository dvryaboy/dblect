"""Shared setup for invoking dbt against an ephemeral DuckDB.

These helpers stand up the environment a dbt invocation needs (a profile pointing
at an ephemeral warehouse, a clean project tree) and run the CLI. Both the
model-running harness (:mod:`dblect.execution.run`) and the incremental
world-compiler (:mod:`dblect.execution.incremental`) build on them, so they live
here rather than inside either consumer.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]


def profile_name_from_project(project_yml: Path) -> str:
    """The ``profile:`` a ``dbt_project.yml`` names, the key its profiles entry uses."""
    cfg: Any = yaml.safe_load(project_yml.read_text())
    if not isinstance(cfg, Mapping):
        raise ValueError(f"{project_yml} does not parse to a mapping")
    cfg_typed: Mapping[str, object] = cast("Mapping[str, object]", cfg)
    profile = cfg_typed.get("profile")
    if not isinstance(profile, str) or not profile:
        raise ValueError(f"{project_yml} missing or invalid `profile:` key")
    return profile


def purge_dbt_outputs(project: Path) -> None:
    """Remove `target/`, `logs/`, and `dbt_packages/` so a run starts clean."""
    for name in ("target", "logs", "dbt_packages"):
        stale = project / name
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)


def write_profile(profiles_dir: Path, profile_name: str, duckdb_path: Path) -> None:
    """Write a one-target ``profiles.yml`` pointing ``profile_name`` at an ephemeral
    DuckDB file."""
    content = (
        f"{profile_name}:\n"
        f"  target: dev\n"
        f"  outputs:\n"
        f"    dev:\n"
        f"      type: duckdb\n"
        f"      path: {duckdb_path}\n"
        f"      threads: 2\n"
    )
    (profiles_dir / "profiles.yml").write_text(content)


def run_dbt(
    dbt_executable: str,
    args: Sequence[str],
    *,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Invoke the dbt CLI, capturing output and never raising on a non-zero exit."""
    return subprocess.run(
        [dbt_executable, *args],
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
