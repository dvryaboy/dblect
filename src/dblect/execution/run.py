"""Run dbt models end-to-end in DuckDB and capture the output.

This is the substrate the Tier 0 invariant checks and the runtime PBT loop
sit on. The HANDOFF discussion settles on subprocess as the v1 approach for
fidelity: in-process Jinja rendering is a follow-up when perf bites.

The contract:

* `run_model` takes a project directory, a model name, and an optional
  fixture map (seed/source name → list of row dicts), copies the project to
  a temporary working directory, writes any provided fixtures into ``seeds/``
  as CSV, writes a generated ``profiles.yml`` pointing at an ephemeral
  DuckDB file, runs ``dbt seed`` then ``dbt run --select +<model>``, and
  finally reads the produced table back through the DuckDB driver.
* Subprocess failures during seed or run raise `RunError`, carrying the
  phase, exit code, stdout, and stderr. Compilation errors, missing
  upstreams, and SQL errors all surface this way: dbt formats them into
  stderr and we don't swallow it.
* The DuckDB file lives inside the temporary directory; when the call
  returns, the contents have already been read into the `RunResult` and the
  on-disk file is gone. Callers wanting to keep the artifact pass
  `keep_artifacts_in=`.
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import duckdb
import yaml  # type: ignore[import-untyped]


class RunError(RuntimeError):
    """Raised when a dbt subprocess fails or the produced table can't be read.

    `phase` is one of ``"seed"``, ``"run"``, ``"query"`` so callers can
    branch on what failed without parsing stderr.
    """

    phase: str
    returncode: int
    stdout: str
    stderr: str

    def __init__(self, phase: str, returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(f"dbt {phase} failed (exit {returncode})\n{stderr or stdout}")
        self.phase = phase
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class RunResult:
    """The output of one ``run_model`` invocation."""

    model_name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    seed_stdout: str
    seed_stderr: str
    run_stdout: str
    run_stderr: str

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def as_dicts(self) -> tuple[Mapping[str, Any], ...]:
        """Rows as `dict` mappings keyed by column name."""
        return tuple(dict(zip(self.columns, row, strict=True)) for row in self.rows)


def run_model(
    project_dir: Path,
    model_name: str,
    *,
    fixtures: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    profile_name: str | None = None,
    dbt_executable: str = "dbt",
    keep_artifacts_in: Path | None = None,
) -> RunResult:
    """Compile and run a dbt model against DuckDB, returning the output rows.

    Args:
        project_dir: Path to a dbt project (the directory containing
            ``dbt_project.yml``). It is copied to a temporary directory so
            seeds and target outputs don't pollute it.
        model_name: dbt name of the model to materialise (e.g., ``"customers"``).
            Upstream seeds and models are built automatically via the ``+`` selector.
        fixtures: Optional mapping of seed name → list of row dicts. Each
            mapped seed is rewritten as a CSV under ``seeds/`` before
            ``dbt seed`` runs; unmapped seeds use the project's existing CSVs.
        profile_name: Profile name to write into the generated ``profiles.yml``.
            Defaults to the ``profile:`` value in ``dbt_project.yml``.
        dbt_executable: Path or name of the dbt CLI. Useful when dbt is
            installed in a sibling environment.
        keep_artifacts_in: If provided, copy the run's DuckDB file there
            before the temporary directory is cleaned up.

    Returns:
        A `RunResult` carrying the model's columns, rows, and the captured
        seed/run subprocess output.

    Raises:
        RunError: If ``dbt seed`` or ``dbt run`` returns non-zero, or if the
            produced table can't be read from DuckDB.
        FileNotFoundError: If `project_dir` doesn't exist or doesn't contain
            ``dbt_project.yml``.
    """
    project_dir = project_dir.resolve()
    project_yml = project_dir / "dbt_project.yml"
    if not project_yml.exists():
        raise FileNotFoundError(f"dbt_project.yml not found in {project_dir}")
    profile = profile_name or _profile_name_from_project(project_yml)

    with tempfile.TemporaryDirectory(prefix="dblect-exec-") as tmp:
        tmp_root = Path(tmp)
        work_project = tmp_root / "project"
        shutil.copytree(project_dir, work_project, dirs_exist_ok=False)
        _purge_dbt_outputs(work_project)
        if fixtures is not None:
            _write_fixture_seeds(work_project, fixtures)

        profiles_dir = tmp_root / "profiles"
        profiles_dir.mkdir()
        duckdb_path = tmp_root / "warehouse.duckdb"
        _write_profile(profiles_dir, profile, duckdb_path)

        env = {**os.environ, "DBT_PROFILES_DIR": str(profiles_dir)}

        seed_proc = _run_dbt(
            dbt_executable,
            ["seed", "--project-dir", str(work_project)],
            env=env,
        )
        if seed_proc.returncode != 0:
            raise RunError("seed", seed_proc.returncode, seed_proc.stdout, seed_proc.stderr)

        run_proc = _run_dbt(
            dbt_executable,
            [
                "run",
                "--project-dir",
                str(work_project),
                "--select",
                f"+{model_name}",
            ],
            env=env,
        )
        if run_proc.returncode != 0:
            raise RunError("run", run_proc.returncode, run_proc.stdout, run_proc.stderr)

        try:
            columns, rows = _query_table(duckdb_path, model_name)
        except duckdb.Error as e:
            raise RunError("query", 0, "", str(e)) from e

        if keep_artifacts_in is not None:
            keep_artifacts_in.mkdir(parents=True, exist_ok=True)
            shutil.copy2(duckdb_path, keep_artifacts_in / duckdb_path.name)

        return RunResult(
            model_name=model_name,
            columns=columns,
            rows=rows,
            seed_stdout=seed_proc.stdout,
            seed_stderr=seed_proc.stderr,
            run_stdout=run_proc.stdout,
            run_stderr=run_proc.stderr,
        )


def _profile_name_from_project(project_yml: Path) -> str:
    cfg: Any = yaml.safe_load(project_yml.read_text())
    if not isinstance(cfg, Mapping):
        raise ValueError(f"{project_yml} does not parse to a mapping")
    cfg_typed: Mapping[str, object] = cast("Mapping[str, object]", cfg)
    profile = cfg_typed.get("profile")
    if not isinstance(profile, str) or not profile:
        raise ValueError(f"{project_yml} missing or invalid `profile:` key")
    return profile


def _purge_dbt_outputs(project: Path) -> None:
    """Remove `target/`, `logs/`, and `dbt_packages/` so each run starts clean."""
    for name in ("target", "logs", "dbt_packages"):
        stale = project / name
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)


def _write_fixture_seeds(
    project: Path, fixtures: Mapping[str, Sequence[Mapping[str, Any]]]
) -> None:
    seeds_dir = project / "seeds"
    seeds_dir.mkdir(exist_ok=True)
    for name, rows in fixtures.items():
        csv_path = seeds_dir / f"{name}.csv"
        _write_csv(csv_path, rows)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        # An empty fixture needs at least a header row; dbt seed can't infer
        # columns from zero rows. We fall back to the existing CSV's header,
        # if any.
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                try:
                    header = next(reader)
                except StopIteration:
                    header = []
            with path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if header:
                    writer.writerow(header)
            return
        path.write_text("")
        return

    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_profile(profiles_dir: Path, profile_name: str, duckdb_path: Path) -> None:
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


def _run_dbt(
    dbt_executable: str,
    args: Sequence[str],
    *,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [dbt_executable, *args],
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )


def _query_table(
    duckdb_path: Path, table: str
) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
    with _duckdb_connect(duckdb_path) as con:
        cursor = con.execute(f"select * from {table}")
        columns = tuple(c[0] for c in cursor.description or [])
        rows = tuple(tuple(r) for r in cursor.fetchall())
        return columns, rows


class _DuckDBConn:
    """Read-only DuckDB connection that closes cleanly on context exit."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._con: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> duckdb.DuckDBPyConnection:
        self._con = duckdb.connect(str(self._path), read_only=True)
        return self._con

    def __exit__(self, *_: object) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None


def _duckdb_connect(path: Path) -> _DuckDBConn:
    return _DuckDBConn(path)
