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

dblect reads the SQL your models produce after dbt's Jinja runtime has rendered them ("compiled SQL"), so it sees macros, conditionals, and refs the way the warehouse will. The two ways to feed it that SQL:

- Pre-run `dbt compile` yourself and let dblect read `target/manifest.json`.
- Let dblect invoke `dbt compile` for you, in which case it needs `dbt-core` installed and a working dbt profile (the same setup `dbt run` needs):

```bash
uv add --dev "dblect[dbt-core]"
```

Finding line numbers refer to the compiled SQL the analyzer parsed, not to the on-disk `.sql` template. Findings always carry the model's source file path, so you can open the source file from the report and locate the construct from there.

## Quick start

Inside any dbt project:

```bash
dblect init
```

This scaffolds `dblect/`, adds dblect to your project dependencies, parses your dbt project, generates editor stubs, and runs the static-analysis audit end-to-end. First findings typically land in under a minute.

See the [demo walkthrough](docs/design/demo_walkthrough.md) for the forward-looking end-to-end tour against `jaffle_shop_duckdb`, or [docs/current_state/architecture.md](docs/current_state/architecture.md) for what's actually built today.

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

Apache 2.0. See [LICENSE](LICENSE).
