"""CLI entry points. The `dblect` console script is registered in pyproject.toml."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="dblect",
    help="Semantic correctness framework for dbt analytics pipelines.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed dblect version."""
    from dblect import __version__

    typer.echo(__version__)


@app.command()
def audit(
    project_dir: Annotated[
        Path,
        typer.Argument(  # pyright: ignore[reportUnknownMemberType]
            help="Path to a dbt project (the directory holding dbt_project.yml).",
        ),
    ] = Path("."),
    manifest: Annotated[
        Path | None,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--manifest",
            help=(
                "Path to a manifest.json. If omitted, dblect looks for "
                "<project_dir>/target/manifest.json and falls back to running "
                "`dbt parse` to produce one."
            ),
        ),
    ] = None,
    dbt_executable: Annotated[
        str,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--dbt-executable",
            help="Name or path of the dbt CLI used by the fallback `dbt parse`.",
        ),
    ] = "dbt",
) -> None:
    """Run the static structural audit over a dbt project's models."""
    from dblect.audit import run_audit
    from dblect.audit.reporter import render_text
    from dblect.manifest import Manifest

    manifest_path = _resolve_manifest_path(
        project_dir=project_dir,
        explicit=manifest,
        dbt_executable=dbt_executable,
    )
    typer.echo(f"audit: reading manifest at {manifest_path}", err=True)
    loaded = Manifest.from_file(manifest_path)
    report = run_audit(loaded)
    typer.echo(render_text(report))


def _resolve_manifest_path(
    *,
    project_dir: Path,
    explicit: Path | None,
    dbt_executable: str,
) -> Path:
    if explicit is not None:
        if not explicit.exists():
            raise typer.BadParameter(f"manifest path does not exist: {explicit}")
        return explicit
    default = project_dir / "target" / "manifest.json"
    if default.exists():
        return default
    if not (project_dir / "dbt_project.yml").exists():
        raise typer.BadParameter(
            f"no dbt_project.yml in {project_dir}; pass --manifest or point at a dbt project"
        )
    if shutil.which(dbt_executable) is None:
        raise typer.BadParameter(
            f"`{dbt_executable}` not on PATH and no manifest at {default}; "
            "install dbt or pass --manifest"
        )
    typer.echo(f"audit: running `{dbt_executable} parse` in {project_dir}", err=True)
    completed = subprocess.run(
        [dbt_executable, "parse", "--project-dir", str(project_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        typer.echo(completed.stdout, err=True)
        typer.echo(completed.stderr, err=True)
        raise typer.Exit(code=completed.returncode)
    if not default.exists():
        raise typer.BadParameter(
            f"`dbt parse` succeeded but {default} is missing; check dbt's target-path config"
        )
    return default


if __name__ == "__main__":
    app()
