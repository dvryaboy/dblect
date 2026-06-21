"""CLI entry points. The `dblect` console script is registered in pyproject.toml."""

from __future__ import annotations

import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from dblect.adapters import AdapterProfile
    from dblect.analysis import AnalysisReport
    from dblect.manifest import Manifest
    from dblect.types import ContractRegistry

app = typer.Typer(
    name="dblect",
    help="Semantic correctness framework for dbt analytics pipelines.",
    no_args_is_help=True,
)


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"
    SARIF = "sarif"


@app.command()
def version() -> None:
    """Print the installed dblect version."""
    from dblect import __version__

    typer.echo(__version__)


@app.command()
def check(
    project_dir: Annotated[
        Path,
        typer.Argument(  # pyright: ignore[reportUnknownMemberType]
            help="Path to a dbt project (the directory holding dbt_project.yml, and dblect/ if present).",
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
            help=(
                "Output format. `text` is for terminals; `json` is for CI / editors; "
                "`sarif` is SARIF 2.1.0 for GitHub code scanning and similar surfaces."
            ),
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
    catalog: Annotated[
        Path | None,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--catalog",
            help=(
                "Path to a catalog.json (`dbt docs generate`). Defaults to "
                "catalog.json alongside the manifest. Supplies seed/source columns "
                "so undocumented DAG leaves resolve; documented columns win on conflict."
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
    resolution_floor: Annotated[
        float | None,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--resolution-floor",
            min=0.0,
            max=1.0,
            help=(
                "Minimum share (0..1) of column references lineage must resolve; "
                "below it a RESOLUTION_BELOW_FLOOR finding fires so thin coverage "
                "is not read as a clean bill."
            ),
        ),
    ] = None,
    base_manifest: Annotated[
        Path | None,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--base-manifest",
            help=(
                "Path to the base revision's manifest.json (for example the stored "
                "production manifest dbt Slim CI keeps). dblect analyses it the same "
                "way as HEAD and reports only the findings the change introduces: a "
                "finding whose kind, model, and subject already exist in the base is "
                "preexisting and filtered out. Because it diffs analysis results "
                "rather than edited source lines, it catches a hazard a change "
                "introduces through a macro or an upstream model without touching the "
                "model's own file. Omitted, the full report is rendered."
            ),
        ),
    ] = None,
    base_catalog: Annotated[
        Path | None,
        typer.Option(  # pyright: ignore[reportUnknownMemberType]
            "--base-catalog",
            help=(
                "Path to the base revision's catalog.json. Defaults to a catalog.json "
                "beside --base-manifest. Only meaningful together with --base-manifest."
            ),
        ),
    ] = None,
) -> None:
    """Check a dbt project: structural hazards and declaration-level contracts.

    Both detector families run over the project. The structural family needs only the
    compiled SQL, so it reports on any project. The declaration family resolves the
    contracts under ``<project_dir>/dblect/`` if present; with none declared it simply
    reports zero contracts resolved rather than nothing to do.
    """
    from dataclasses import replace

    from dblect import __version__
    from dblect.analysis import analyze
    from dblect.baseline import introduced_findings
    from dblect.loader import load_declarations
    from dblect.report import render_json, render_text
    from dblect.sarif import render_sarif

    if base_catalog is not None and base_manifest is None:
        raise typer.BadParameter("--base-catalog has no effect without --base-manifest")

    manifest_path = _resolve_manifest_path(
        project_dir=project_dir, explicit=manifest, dbt_executable=dbt_executable, command="check"
    )
    loaded = _load_manifest(manifest_path=manifest_path, explicit_catalog=catalog, command="check")
    profile = _resolve_profile(loaded.adapter_type, dialect_override, command="check")

    load_result = load_declarations(project_dir)
    report = analyze(
        loaded, profile, registry=load_result.registry, resolution_floor=resolution_floor
    )
    report = replace(report, check=replace(report.check, load_issues=load_result.issues))
    if base_manifest is not None:
        base = _analyze_base(
            base_manifest=base_manifest,
            base_catalog=base_catalog,
            dialect_override=dialect_override,
            registry=load_result.registry,
            resolution_floor=resolution_floor,
        )
        # Narrow only the merged ``findings`` view: that is what every renderer and the
        # exit code read. The family sub-reports keep their project-wide coverage
        # metadata (models scanned, contracts resolved), which describes the whole run,
        # not the introduced subset.
        report = replace(report, findings=introduced_findings(report.findings, base.findings))
    match output_format:
        case OutputFormat.JSON:
            rendered = render_json(report)
        case OutputFormat.SARIF:
            rendered = render_sarif(report, version=__version__)
        case OutputFormat.TEXT:
            rendered = render_text(report)
    typer.echo(rendered)
    if (report.findings or report.check.load_issues) and not no_fail:
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


def _analyze_base(
    *,
    base_manifest: Path,
    base_catalog: Path | None,
    dialect_override: str | None,
    registry: ContractRegistry,
    resolution_floor: float | None,
) -> AnalysisReport:
    """Analyse the base revision's manifest the same way HEAD was analysed.

    Same dialect resolution, same declarations (``registry``), and the same
    resolution floor, so a finding's cross-world identity is comparable across the two
    worlds. The base catalog defaults to a ``catalog.json`` beside ``base_manifest``,
    matching the Slim CI layout where the stored manifest and catalog sit together.
    """
    from dblect.analysis import analyze

    if not base_manifest.exists():
        raise typer.BadParameter(f"--base-manifest path does not exist: {base_manifest}")
    loaded = _load_manifest(
        manifest_path=base_manifest, explicit_catalog=base_catalog, command="check (base)"
    )
    profile = _resolve_profile(loaded.adapter_type, dialect_override, command="check (base)")
    return analyze(loaded, profile, registry=registry, resolution_floor=resolution_floor)


def _resolve_profile(
    adapter_type: str, dialect_override: str | None, *, command: str
) -> AdapterProfile:
    """Resolve the run's single target profile, turning an unvalidated adapter into
    an actionable CLI error and warning when the resolved target is best-effort."""
    from dblect.adapters import (
        UnvalidatedAdapterError,
        resolve_profile,
        validated_adapters,
    )

    try:
        profile = resolve_profile(adapter_type=adapter_type, explicit_dialect=dialect_override)
    except UnvalidatedAdapterError as e:
        raise typer.BadParameter(
            f"manifest adapter is `{e.adapter_type}`, not in dblect's validated set "
            f"({sorted(validated_adapters())}); pass --dialect <name> to force a target "
            f"(interpretation will be best-effort)."
        ) from e
    if not profile.validated:
        typer.echo(
            f"{command}: using unvalidated target `{profile.adapter_type}` "
            f"(dialect `{profile.sqlglot_dialect}`, validated: {sorted(validated_adapters())}); "
            f"behavior is best-effort.",
            err=True,
        )
    return profile


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


def _resolve_catalog_path(*, explicit: Path | None, manifest_path: Path) -> Path | None:
    """The ``catalog.json`` to read, or ``None`` when there is none.

    An explicit path must exist (a typo should fail loudly). Otherwise dblect
    looks for ``catalog.json`` next to the manifest, where ``dbt docs generate``
    writes it, and a miss is silent: the catalog is optional, the run proceeds on
    documented and derived columns alone."""
    if explicit is not None:
        if not explicit.exists():
            raise typer.BadParameter(f"catalog path does not exist: {explicit}")
        return explicit
    default = manifest_path.parent / "catalog.json"
    return default if default.exists() else None


def _load_manifest(*, manifest_path: Path, explicit_catalog: Path | None, command: str) -> Manifest:
    """Load the manifest and, when a catalog is available, merge its
    warehouse-introspected columns so seeds and sources resolve without manual
    documentation. A missing catalog is noted, not an error: it is the difference
    between full leaf coverage and resolving only what ``schema.yml`` documents."""
    from dblect.manifest import Catalog, Manifest

    typer.echo(f"{command}: reading manifest at {manifest_path}", err=True)
    loaded = Manifest.from_file(manifest_path)
    catalog_path = _resolve_catalog_path(explicit=explicit_catalog, manifest_path=manifest_path)
    if catalog_path is not None:
        typer.echo(f"{command}: reading catalog at {catalog_path}", err=True)
        return loaded.merge_catalog(Catalog.from_file(catalog_path))
    typer.echo(
        f"{command}: no catalog.json alongside the manifest; seed/source columns come "
        "only from schema.yml, so undocumented leaves may not resolve. Run "
        "`dbt docs generate` or pass --catalog to cover them.",
        err=True,
    )
    return loaded


if __name__ == "__main__":
    app()
