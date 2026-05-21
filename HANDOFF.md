# Handoff — next-session pickup

*For a new Claude Code session starting from this commit. Read this top-to-bottom, then jump into one of the two tasks below.*

## What's done

- All design docs landed (`docs/`), the decisions log is the source of truth (`questions_and_decisions.md`), and the demo walkthrough sketches the v1 target (`docs/demo_walkthrough.md`).
- Project scaffolding works end-to-end: `uv sync && uv run pytest && uv run ruff check && uv run pyright` all pass on a fresh clone.
- **Phase 1, item 1** (dbt manifest ingestion) is complete: `src/dblect/manifest/` with `parse.py` (Manifest/Node/Column/ResourceType) and `dag.py` (immutable DAG with cycle detection, deterministic topological order, transitive queries). 27 tests pass, 92% coverage.
- Vendored test fixture at `tests/fixtures/jaffle/manifest.json` (real `dbt parse` output against `../jaffle_shop_duckdb`). Refresh via `scripts/refresh_jaffle_fixtures.sh`.

## Read first

In order of relevance to picking up work:

1. **`CLAUDE.md`** — project coding norms (rigorous types, PBT, no test theater, no comment bloat).
2. **`questions_and_decisions.md`** — every design decision is here. Don't relitigate; if you think something needs to change, surface it before changing it.
3. **`docs/tiers_and_rough_implementation_order.md`** — the implementation sequence. Items 2 and 3 below are Phase 1 items 2 and 3 from this doc.
4. **`docs/demo_walkthrough.md`** — the v1 target. Every Phase 1 deliverable should be measured against "does this move us closer to making this walkthrough actually work?"
5. **`src/dblect/manifest/parse.py` and `dag.py`** — the patterns to follow for new modules (dataclasses with `frozen=True, slots=True`, typed boundaries against untyped deps via `cast`/`type: ignore`, no comments-as-narration).

## How to run things

```bash
uv sync                                # one-time setup
uv run pytest                          # all tests
uv run pytest tests/manifest -v        # one subtree
uv run ruff check                      # lint
uv run ruff format                     # apply formatting
uv run pyright                         # strict type check
scripts/refresh_jaffle_fixtures.sh     # regenerate vendored manifest
```

CI runs the same on Python 3.11/3.12/3.13 via `.github/workflows/ci.yml`.

## Open task: Phase 1, item 2 — SQL static analysis layer

**Goal.** A sqlglot wrapper exposing the AST patterns the Tier 0 audit and (later) the column-lineage engine will care about: joins, aggregations, window functions, ordering structures, NULL handling. The static-detector findings in the demo walkthrough's Step 0 ("NULL-group risk in `customers.sql`") come from this layer.

**Scope.**

- A `dblect.sql` subpackage that takes a SQL string + dialect and exposes:
  - The parsed sqlglot AST (or a thin wrapper if we want a stable internal AST shape — see open question below).
  - Named queries over it: "list joins," "list window functions with their ORDER BY clauses," "list GROUP BY targets," "list aggregations and their sources," "find subqueries that LEFT JOIN then GROUP BY a column from the right side."
- Coverage of the SQL Tier 0 will need:
  - `ORDER BY`, `ROW_NUMBER`, `FIRST_VALUE`, `LAG`, `LEAD`, `ARRAY_AGG` patterns.
  - LEFT JOIN → GROUP BY → NULL-group detection (the jaffle `customers.sql` case).
  - `COALESCE` over a join key (NULL-shadowing risk).
  - Integer division on columns of cents-like provenance (mostly a downstream concern once types exist, but the structural pattern is detectable here).
- Tests against jaffle's `raw_code` (Jinja-laden SQL).

**Inputs available.** `Node.raw_code` is populated for jaffle (verified). `Node.compiled_code` is None because we only ran `dbt parse`, not `dbt compile`. The static analysis at this layer should work on `raw_code`; Jinja rendering and Jinja-AST walking are Phase 3 concerns (see `docs/var-inference-spec.md` and the flags doc).

**One open design decision before you start.** Per `dblect_technical_intro.md`'s "Open questions" section: does dblect's static analyzer use sqlglot's AST directly, or does it produce its own internal AST that converts to sqlglot for the SQL-compilation path? The right call is probably "use sqlglot directly for this layer; if/when we need a stable internal AST for proxy expressions, that's a separate concern at Tier 1+." But this should be settled in writing before writing code. **Update `questions_and_decisions.md` with the call and rationale, then implement.**

**Patterns to follow (from manifest module).**

- `src/dblect/sql/` with submodules per concern (`parse.py`, `patterns.py`, etc.); single `__init__.py` re-exports.
- `dataclass(frozen=True, slots=True)` for value types. Plain `@property` not `cached_property` (slots make caching infeasible).
- `StrEnum` for any closed vocabulary.
- Type-check the boundary into sqlglot; sqlglot ships type stubs and is mostly clean, but expect occasional `cast` or `type: ignore[...]` at integration points and document why with a comment.
- Tests: unit tests for each pattern detector, plus PBT where it fits (e.g., "any query without ORDER BY produces no ordering hazards"; "round-trip parse → render → re-parse is idempotent for a class of inputs").

**Done when.**

- `dblect.sql.patterns` (or similar) exposes detectors that find the NULL-group risk in jaffle's `customers.sql`. Test asserts this concretely against the fixture.
- All other Tier 0 SQL-AST detectors (ordering hazards, COALESCE-on-key, fanout indicators) have at least skeleton implementations and unit tests, even if some are deferred-and-stubbed.
- `uv run pytest && uv run ruff check && uv run pyright` all green.
- Coverage stays at 90%+ overall.

## Open task: Phase 1, item 3 — DuckDB execution harness

**Goal.** Run dbt models in DuckDB against generated data and capture the output reliably. The substrate the Tier 0 invariant checks and (later) the runtime PBT loop sit on.

**Scope.**

- A `dblect.execution` subpackage that:
  - Takes a parsed `Manifest`, a target model, and a fixture (mapping of upstream model/source/seed name → row data).
  - Materializes the fixture as DuckDB tables.
  - Compiles the target model's `raw_code` against the fixture (renders Jinja with `ref()` resolving to the materialized tables, no warehouse needed).
  - Executes the resulting SQL in DuckDB.
  - Returns the output rows in a typed shape.
- Robust enough to:
  - Run jaffle's five models end-to-end against the seeds (`raw_customers`, `raw_orders`, `raw_payments`).
  - Detect and surface dbt-duckdb adapter quirks rather than swallowing them.
  - Survive the cases where `dbt parse`'s manifest doesn't include compiled SQL (which is our current jaffle fixture's state).

**Where the engineering actually lives.** dbt-duckdb is the adapter, but driving dbt programmatically inside another Python process is fiddly. Two approaches:

1. **Subprocess `dbt run --select <model> --vars '{...}'`** against a copied jaffle project pointed at an ephemeral DuckDB file. Highest fidelity, slowest, requires `dbt-core` as a real (not optional) dep when this path runs.
2. **In-process: render Jinja ourselves, execute the resulting SQL in DuckDB directly.** Faster, no `dbt-core` runtime cost, but we have to reimplement enough of dbt's Jinja context (the `ref`, `source`, `var`, `config` macros) for the SQL to render. Doable; bounded; see `docs/var-inference-spec.md` for the same Jinja-walking pattern flag discovery uses.

The contract-directed-generation doc and the flags doc both punt this question with "default to subprocess, offer in-process as opt-in fast mode for local iteration." That's the v1 stance: **start with subprocess; treat in-process as a known follow-up if perf becomes an issue.**

**Tests.**

- A real "run all five jaffle models against the vendored seeds, assert output row counts" test.
- A "generate degenerate input (empty seeds), assert sensible behavior" test — this is the seed of the Tier 0 heuristic-invariant check the audit will eventually run.
- Both should run in CI without warehouse credentials.

**Footguns to expect.**

- `dbt-core` + `dbt-duckdb` versions: the jaffle project pins `dbt-duckdb>=1.10.1` and `dbt-core>=1.11.0`. Our pyproject.toml has `dbt-core>=1.8` as an optional extra. Verify the combination works.
- DuckDB file-vs-memory tradeoffs: in-memory is faster but doesn't persist between dbt invocations, which the subprocess approach needs.
- `profiles.yml`: dbt looks for it in `~/.dbt/profiles.yml` or via `DBT_PROFILES_DIR`. The harness needs to generate one pointing at the ephemeral DuckDB file.
- Jaffle uses seeds; `dbt seed` must run before `dbt run` for the first invocation.

**Done when.**

- `dblect.execution.run_model(manifest, model_id, fixtures) -> Output` (or similar) successfully runs jaffle's `customers` model and returns the expected ~100 rows.
- The harness is tested in CI without any warehouse credentials.
- The contract is clear about what kinds of failures it surfaces (DuckDB SQL errors, dbt compile errors, missing upstreams) vs swallows.
- `uv run pytest && uv run ruff check && uv run pyright` all green.

## Order recommendation

Do item 2 first. Reasons:

- The static-analysis layer is independently useful (it powers Tier 0 detectors that don't need execution at all), so it ships value on its own.
- It doesn't depend on item 3, so you can land it with no DuckDB-adapter rabbit-holing.
- Once item 2 lands, item 3 can be tested by comparing static expectations to executed reality (a useful symmetry).

## Footguns from this session worth remembering

- **`@dataclass(frozen=True, slots=True)` is incompatible with `@cached_property`.** Slots remove `__dict__`; frozen prevents attribute mutation. Use plain `@property` and recompute, or drop `slots=True` if caching matters more than memory.
- **`dbt-artifacts-parser` is untyped.** Its `parse_manifest` import needs `# type: ignore[import-untyped]`. Its node objects expose `resource_type` as a bare string in v12 (despite the schema suggesting an enum), so wrap defensively: `ResourceType.from_raw(str(n.resource_type))`.
- **The standard Python `.gitignore` includes `MANIFEST`** (an old setuptools sdist file). On case-insensitive macOS/Windows filesystems this swallows our `src/dblect/manifest/` package. The fix lands in this commit: `/MANIFEST` (root-anchored) instead of bare `MANIFEST`.
- **`uv pip install <pkg>`** (without it being in `pyproject.toml`) installs into the active venv but doesn't update `pyproject.toml` or the lockfile. The next `uv sync --frozen` will quietly uninstall it. Use `uv add` if you mean to add a dep; use `uv pip install` only for genuine one-offs (and accept the cleanup).
- **`hypothesis` PBT tests** generate cached examples under `.hypothesis/` — already gitignored, but worth noting if you wonder where the regression examples are.

## Repo state summary

```
.
├── .github/workflows/ci.yml
├── CHANGELOG.md
├── CLAUDE.md
├── HANDOFF.md                          ← you are here
├── LICENSE
├── README.md
├── docs/                               ← design + decisions
├── pyproject.toml
├── questions_and_decisions.md          ← source of truth
├── scripts/refresh_jaffle_fixtures.sh
├── src/dblect/
│   ├── __init__.py
│   ├── _version.py
│   ├── audit/                          ← stub (Phase 2)
│   ├── cli/                            ← typer skeleton
│   ├── execution/                      ← stub (Phase 1 item 3, OPEN)
│   ├── manifest/                       ← done (Phase 1 item 1)
│   ├── py.typed
│   └── sql/                            ← stub (Phase 1 item 2, OPEN)
├── tests/
│   ├── conftest.py
│   ├── fixtures/jaffle/manifest.json   ← vendored
│   ├── manifest/test_dag.py            ← 14 tests inc. 3 PBT
│   ├── manifest/test_parse.py          ← 11 tests on jaffle
│   └── test_smoke.py
└── uv.lock
```

Good luck.
