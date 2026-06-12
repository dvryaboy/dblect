"""CLI entry points. The `dblect` console script is registered in pyproject.toml."""

from __future__ import annotations

import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="dblect",
    help="Semantic correctness framework for dbt analytics pipelines.",
    no_args_is_help=True,
)


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


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
                "`dbt compile` to produce one."
            ),
        ),
    ] = None,
    dbt_executable: Annotated[
        str,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--dbt-executable",
            help="Name or path of the dbt CLI used by the fallback `dbt compile`.",
        ),
    ] = "dbt",
    output_format: Annotated[
        OutputFormat,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--format",
            "-f",
            help="Output format. `text` is for terminals; `json` is for CI / editors.",
        ),
    ] = OutputFormat.TEXT,
    dialect_override: Annotated[
        str | None,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--dialect",
            help=(
                "Force a sqlglot dialect for SQL parsing, overriding the "
                "manifest's adapter_type. Required when running against an "
                "adapter dblect has not validated; passing the flag is the "
                "operator's acknowledgment that detector behavior is best-effort."
            ),
        ),
    ] = None,
    no_fail: Annotated[
        bool,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--no-fail",
            help=(
                "Always exit 0, even when findings exist. Default is to exit 1 "
                "if any unsuppressed finding is reported."
            ),
        ),
    ] = False,
) -> None:
    """Run the static structural audit over a dbt project's models."""
    from dblect.audit import run_audit
    from dblect.audit.reporter import render_json, render_text
    from dblect.manifest import Manifest
    from dblect.sql.dialects import (
        VALIDATED_DIALECTS,
        UnvalidatedAdapterError,
        resolve_dialect,
    )

    manifest_path = _resolve_manifest_path(
        project_dir=project_dir,
        explicit=manifest,
        dbt_executable=dbt_executable,
        command="audit",
    )
    typer.echo(f"audit: reading manifest at {manifest_path}", err=True)
    loaded = Manifest.from_file(manifest_path)
    try:
        dialect = resolve_dialect(
            adapter_type=loaded.adapter_type,
            explicit_dialect=dialect_override,
        )
    except UnvalidatedAdapterError as e:
        raise typer.BadParameter(
            f"manifest adapter is `{e.adapter_type}`, which is not in "
            f"dblect's validated set ({sorted(VALIDATED_DIALECTS)}). "
            f"Pass --dialect <name> to force a sqlglot dialect "
            f"(interpretation will be best-effort)."
        ) from e
    if dialect not in VALIDATED_DIALECTS:
        typer.echo(
            f"audit: using unvalidated dialect `{dialect}` "
            f"(validated: {sorted(VALIDATED_DIALECTS)}); "
            f"detector behavior is best-effort.",
            err=True,
        )
    report = run_audit(loaded, dialect=dialect)
    rendered = render_json(report) if output_format is OutputFormat.JSON else render_text(report)
    typer.echo(rendered)
    if report.findings and not no_fail:
        raise typer.Exit(code=1)


@app.command()
def check(
    project_dir: Annotated[
        Path,
        typer.Argument(  # pyright: ignore[reportUnknownMemberType]
            help="Path to a dbt project (the directory holding dbt_project.yml and dblect/).",
        ),
    ] = Path("."),
    manifest: Annotated[
        Path | None,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--manifest",
            help="Path to a manifest.json. Defaults to <project_dir>/target/manifest.json.",
        ),
    ] = None,
    dbt_executable: Annotated[
        str,
        typer.Option("--dbt-executable", help="dbt CLI for the fallback `dbt compile`."),  # pyright: ignore[reportUnknownMemberType]
    ] = "dbt",
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="`text` for terminals, `json` for CI / editors."),  # pyright: ignore[reportUnknownMemberType]
    ] = OutputFormat.TEXT,
    dialect_override: Annotated[
        str | None,
        typer.Option("--dialect", help="Force a sqlglot dialect, overriding the manifest."),  # pyright: ignore[reportUnknownMemberType]
    ] = None,
    no_fail: Annotated[
        bool,
        typer.Option("--no-fail", help="Always exit 0, even when findings exist."),  # pyright: ignore[reportUnknownMemberType]
    ] = False,
) -> None:
    """Load a project's contracts, propagate, and report declaration-level findings."""
    from dataclasses import replace

    from dblect.check import render_json, render_text, run_check
    from dblect.loader import load_declarations
    from dblect.manifest import Manifest

    manifest_path = _resolve_manifest_path(
        project_dir=project_dir, explicit=manifest, dbt_executable=dbt_executable, command="check"
    )
    typer.echo(f"check: reading manifest at {manifest_path}", err=True)
    loaded = Manifest.from_file(manifest_path)
    dialect = _resolve_dialect(loaded.adapter_type, dialect_override)

    load_result = load_declarations(project_dir)
    report = replace(
        run_check(loaded, registry=load_result.registry, dialect=dialect),
        load_issues=load_result.issues,
    )
    rendered = render_json(report) if output_format is OutputFormat.JSON else render_text(report)
    typer.echo(rendered)
    if report.has_findings and not no_fail:
        raise typer.Exit(code=1)


@app.command()
def init(
    project_dir: Annotated[
        Path,
        typer.Argument(  # pyright: ignore[reportUnknownMemberType]
            help="Path to the dbt project to scaffold a dblect/ tree in.",
        ),
    ] = Path("."),
    manifest: Annotated[
        Path | None,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--manifest",
            help="Path to a manifest.json the generated `models` stubs are built from.",
        ),
    ] = None,
    dbt_executable: Annotated[
        str,
        typer.Option("--dbt-executable", help="dbt CLI for the fallback `dbt compile`."),  # pyright: ignore[reportUnknownMemberType]
    ] = "dbt",
) -> None:
    """Scaffold a project's dblect/ declaration tree and generate the models stubs."""
    from dblect.contracts.stubs import generate_stub_module
    from dblect.manifest import Manifest

    manifest_path = _resolve_manifest_path(
        project_dir=project_dir, explicit=manifest, dbt_executable=dbt_executable, command="init"
    )
    loaded = Manifest.from_file(manifest_path)

    decl = project_dir / "dblect"
    created = _scaffold_declarations(decl)
    stubs_dir = decl / "_stubs"
    stubs_dir.mkdir(parents=True, exist_ok=True)
    (stubs_dir / "__init__.py").write_text("")
    stubs_path = stubs_dir / "models.py"
    stubs_path.write_text(generate_stub_module(loaded))

    for path in (*created, stubs_path):
        typer.echo(f"init: wrote {path}")
    typer.echo("init: scaffolding complete; add contracts and run `dblect check`.")


_STARTER_TYPES = '"""Project domain types: DomainType subclasses and named refinements."""\n'
_STARTER_CONTRACTS_INIT = '"""Model contracts: one ModelContract per dbt model you type."""\n'
_GITIGNORE = "# Generated by dblect; not checked in.\n_stubs/\n"


def _scaffold_declarations(decl: Path) -> list[Path]:
    """Create the declaration tree, never overwriting a file the user may have
    edited. Returns the files this run created."""
    decl.mkdir(parents=True, exist_ok=True)
    files = {
        decl / "__init__.py": "",
        decl / "types.py": _STARTER_TYPES,
        decl / "contracts" / "__init__.py": _STARTER_CONTRACTS_INIT,
        decl / ".gitignore": _GITIGNORE,
    }
    created: list[Path] = []
    for path, body in files.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        created.append(path)
    return created


def _resolve_dialect(adapter_type: str, dialect_override: str | None) -> str:
    from dblect.sql.dialects import (
        VALIDATED_DIALECTS,
        UnvalidatedAdapterError,
        resolve_dialect,
    )

    try:
        dialect = resolve_dialect(adapter_type=adapter_type, explicit_dialect=dialect_override)
    except UnvalidatedAdapterError as e:
        raise typer.BadParameter(
            f"manifest adapter is `{e.adapter_type}`, not in dblect's validated set "
            f"({sorted(VALIDATED_DIALECTS)}); pass --dialect <name> to force one."
        ) from e
    if dialect not in VALIDATED_DIALECTS:
        typer.echo(f"using unvalidated dialect `{dialect}`; behavior is best-effort.", err=True)
    return dialect


def _resolve_manifest_path(
    *,
    project_dir: Path,
    explicit: Path | None,
    dbt_executable: str,
    command: str,
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
            "install dbt (e.g. `uv add 'dblect[dbt-core]'`) or pass --manifest"
        )
    typer.echo(f"{command}: running `{dbt_executable} compile` in {project_dir}", err=True)
    completed = subprocess.run(
        [dbt_executable, "compile", "--project-dir", str(project_dir)],
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
            f"`dbt compile` succeeded but {default} is missing; check dbt's target-path config"
        )
    return default


if __name__ == "__main__":
    app()
