# dblect

A semantic correctness framework for dbt analytics pipelines. Adds a typed declaration layer on top of your existing dbt project to catch a class of bugs where tests pass, the build is green, and the meaning of a column has quietly shifted: revenue switched from net to gross, an attribution window changed, a discount field started including coupons.

dbt tests, Great Expectations, and Monte Carlo cover value-level checks well; dblect covers meaning-level checks.

## Status

Pre-alpha. Design is settled; implementation is in progress. See [docs/](docs/) for the design notes and [questions_and_decisions.md](questions_and_decisions.md) for the decisions log.

## Install

```bash
uv add --dev dblect
```

Or with pip:

```bash
pip install dblect
```

For projects that want dblect to invoke `dbt parse` for them (instead of requiring an existing `target/manifest.json`):

```bash
uv add --dev "dblect[dbt-core]"
```

## Quick start

Inside any dbt project:

```bash
dblect init
```

This scaffolds `dblect/`, adds dblect to your project dependencies, parses your dbt project, generates editor stubs, and runs the static-analysis audit end-to-end. First findings typically land in under a minute.

See the [demo walkthrough](docs/demo_walkthrough.md) for an end-to-end tour against `jaffle_shop_duckdb`.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                  # install dev environment
uv run pytest            # run tests
uv run ruff check        # lint
uv run ruff format       # format
uv run pyright           # type-check (strict)
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
