"""Compile a dbt project into its incremental worlds, data-free.

A dbt incremental model compiles two ways: a full-refresh form whose
``{% if is_incremental() %}`` branch is absent, and a steady-state form where it
is present. A single manifest captures one. This module produces both from
``dbt compile`` alone, with no build and no connection to the project's real
warehouse, by shadowing ``is_incremental()`` with a constant-returning macro and
compiling once per value against an ephemeral DuckDB connection.

``ref()`` and ``{{ this }}`` resolve to relation names at parse, so the
steady-state SELECT compiles even though nothing has been built. Each world is
read back through the ordinary :class:`~dblect.manifest.Manifest` reader, so a
world is just a ``Manifest`` the rest of the pipeline already understands.

The reach of the override is the bare ``{{ is_incremental() }}`` call. A model
that calls ``dbt.is_incremental()`` explicitly, or a branch that introspects the
existing relation's schema at compile, is not reached by the data-free path; the
compile that does not reach it degrades rather than misleads (its world is
reported with an error and the other world still stands).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from dblect.execution.project_env import (
    profile_name_from_project,
    purge_dbt_outputs,
    run_dbt,
    write_profile,
)
from dblect.lineage.facts.model import WorldRef
from dblect.manifest import Manifest

# The run-mode axis and its two worlds, expressed as the same ``WorldRef``
# assignment vocabulary the flag layer uses, so an incremental world composes
# with flag worlds as a union of assignments rather than a separate notion.
INCREMENTAL_AXIS = "is_incremental"
FULL_REFRESH_WORLD: WorldRef = WorldRef(frozenset({(INCREMENTAL_AXIS, False)}))
STEADY_STATE_WORLD: WorldRef = WorldRef(frozenset({(INCREMENTAL_AXIS, True)}))

# The injected override lives under the project's macro path. The name is
# distinctive so it reads as dblect's in a copied project and is unlikely to
# collide with a project macro file.
_OVERRIDE_MACRO_FILE = "dblect_is_incremental_override.sql"


@dataclass(frozen=True, slots=True)
class CompiledWorld:
    """One world's compilation: its ``WorldRef`` and the harvested ``Manifest``,
    or ``None`` with an ``error`` when that world's compile did not succeed."""

    world: WorldRef
    manifest: Manifest | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.manifest is not None


@dataclass(frozen=True, slots=True)
class IncrementalWorldCompilation:
    """The two incremental worlds of a project."""

    full_refresh: CompiledWorld
    steady_state: CompiledWorld

    def manifests(self) -> Mapping[WorldRef, Manifest]:
        """The successfully compiled worlds, keyed by ``WorldRef``, ready to feed
        per-world analysis. A world whose compile failed is omitted."""
        return {
            world.world: world.manifest
            for world in (self.full_refresh, self.steady_state)
            if world.manifest is not None
        }


def compile_incremental_worlds(
    project_dir: Path, *, dbt_executable: str = "dbt"
) -> IncrementalWorldCompilation:
    """Compile ``project_dir`` into its full-refresh and steady-state worlds.

    The project is copied to a temp directory, an ``is_incremental()`` override is
    injected, and ``dbt compile`` is run once per world against an ephemeral
    DuckDB connection. No seed, no run, no warehouse data, and no connection to
    the project's real target. Returns both worlds; a world whose compile failed
    carries the dbt error rather than a manifest.
    """
    project_dir = project_dir.resolve()
    project_yml = project_dir / "dbt_project.yml"
    if not project_yml.exists():
        raise FileNotFoundError(f"dbt_project.yml not found in {project_dir}")
    profile = profile_name_from_project(project_yml)
    macro_subdir = _first_macro_path(project_yml)

    with tempfile.TemporaryDirectory(prefix="dblect-worlds-") as tmp:
        tmp_root = Path(tmp)
        work = tmp_root / "project"
        shutil.copytree(project_dir, work, dirs_exist_ok=False)
        purge_dbt_outputs(work)

        profiles_dir = tmp_root / "profiles"
        profiles_dir.mkdir()
        duckdb_path = tmp_root / "warehouse.duckdb"
        write_profile(profiles_dir, profile, duckdb_path)
        env = {**os.environ, "DBT_PROFILES_DIR": str(profiles_dir)}

        macro_dir = work / macro_subdir
        macro_dir.mkdir(parents=True, exist_ok=True)
        macro_path = macro_dir / _OVERRIDE_MACRO_FILE

        # The two compiles share one work tree, override file, and
        # ``target/manifest.json``, so they must run sequentially: each harvests its
        # manifest into memory before the next overwrites the override and the
        # output. Parallelizing them would need separate work trees per world.
        return IncrementalWorldCompilation(
            full_refresh=_compile_world(
                work, macro_path, FULL_REFRESH_WORLD, value=False, dbt=dbt_executable, env=env
            ),
            steady_state=_compile_world(
                work, macro_path, STEADY_STATE_WORLD, value=True, dbt=dbt_executable, env=env
            ),
        )


def _compile_world(
    work: Path,
    macro_path: Path,
    world: WorldRef,
    *,
    value: bool,
    dbt: str,
    env: Mapping[str, str],
) -> CompiledWorld:
    """Write the override for ``value``, compile, and harvest the manifest. A
    non-zero compile or a missing manifest yields an opaque world, never a raise."""
    macro_path.write_text(_override_macro(value))
    proc = run_dbt(dbt, ["compile", "--project-dir", str(work)], env=env)
    if proc.returncode != 0:
        return CompiledWorld(world=world, manifest=None, error=_dbt_error(proc.stdout, proc.stderr))
    manifest_path = work / "target" / "manifest.json"
    if not manifest_path.exists():
        return CompiledWorld(world=world, manifest=None, error="compile produced no manifest.json")
    return CompiledWorld(world=world, manifest=Manifest.from_file(manifest_path))


def _override_macro(value: bool) -> str:
    literal = "true" if value else "false"
    return "{% macro is_incremental() %}{{ return(" + literal + ") }}{% endmacro %}\n"


def _first_macro_path(project_yml: Path) -> str:
    """The project's first declared macro path, defaulting to dbt's ``macros``."""
    cfg: Any = yaml.safe_load(project_yml.read_text())
    if isinstance(cfg, Mapping):
        raw = cast("Mapping[str, object]", cfg).get("macro-paths")
        if isinstance(raw, list) and raw and isinstance(raw[0], str) and raw[0]:
            return raw[0]
    return "macros"


def _dbt_error(stdout: str, stderr: str, *, limit: int = 2000) -> str:
    """The tail of dbt's output, where the compile error is reported."""
    text = (stderr or stdout).strip()
    return text[-limit:]
