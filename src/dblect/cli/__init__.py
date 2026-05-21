"""CLI entry points. The `dblect` console script is registered in pyproject.toml."""

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


if __name__ == "__main__":
    app()
