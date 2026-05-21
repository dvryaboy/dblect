# dblect: current architecture

A description of what's built right now, not what's planned. Forward-looking work (typed contracts, runtime PBT, replay-determinism, generator framework, multi-table coordinated generation, MCP) lives in [docs/design/](../design/) and is not implemented yet.

What exists today is the **static structural-hazards analyzer**: one command that reads a dbt project, parses each model's SQL, runs structural detectors over every model, and prints findings the user can navigate to and act on. It earns its keep against a real dbt project before any types or contracts are declared.

## What you can do with it today

```bash
$ dblect audit path/to/dbt-project
audit: scanned 5 models, 1 finding

models/customers.sql  (model.jaffle_shop.customers)
  L44  null_group_after_outer_join
      GROUP BY orders.customer_id references column(s) from nullable
      join side (orders); unmatched rows collapse into a NULL group
      snippet: orders.customer_id
```

Exit code is `1` when unsuppressed findings exist (or `0` with `--no-fail`). `--format json` produces a documented schema for CI consumption.

## Layout

```
src/dblect/
├── manifest/          # parse manifest.json into typed Node + DAG
├── sql/               # parse SQL (with Jinja), walk the AST, detect hazards
├── audit/             # orchestrate detectors over a manifest, render output
├── cli/               # the `dblect` typer app
└── execution/         # run dbt models in DuckDB (used for execution tests; not on the audit path)
```

Each package has a focused job and clean exports through its `__init__.py`. Internals live in `_*.py` modules.

## Data flow

The audit pipeline is a straight line through three layers:

```
dbt project
    │
    │  (a) dbt parse / pre-built target/manifest.json
    ▼
manifest.json (on disk)
    │
    │  (b) dblect.manifest.Manifest.from_file()
    ▼
Manifest (typed)
    │   - Nodes keyed by unique_id
    │   - raw_code for each model
    │   - original_file_path threaded through
    │   - DAG built from depends_on edges
    │
    │  (c) dblect.audit.run_audit()
    ▼
For each model:
    │   - ParsedSQL.parse(raw_code)  ────► sqlglot AST + Jinja placeholder record
    │   - DEFAULT_DETECTORS run over the AST
    │   - parse_directives() reads -- noqa-fixture: comments
    │   - apply() partitions into active / suppressed
    │
    ▼
AuditReport
    │   - findings: tuple[LocatedFinding, ...]
    │   - suppressed: tuple[SuppressedFinding, ...]
    │   - skipped: tuple[SkippedModel, ...]
    │
    │  (d) render_text() or render_json()
    ▼
stdout
```

Status messages (manifest path, dbt parse invocation) go to stderr so JSON consumers can pipe stdout into `jq` without interleaved noise.

## The manifest layer (`src/dblect/manifest/`)

`Manifest.from_file(path)` loads a `manifest.json` and produces a frozen, typed view. Internally it leans on `dbt-artifacts-parser` for the version-aware parse (dbt's manifest schema churns; we don't track that ourselves) and then transforms the parser's pydantic models into dblect's own dataclasses.

Key types in [`manifest/parse.py`](../../src/dblect/manifest/parse.py):

- **`Manifest`** — `schema_version`, `nodes: Mapping[str, Node]`, plus `models` / `sources` / `seeds` / `snapshots` properties that filter by resource type.
- **`Node`** — `unique_id`, `name`, `resource_type`, `fqn`, `package_name`, `schema`, `raw_code`, `compiled_code`, `original_file_path`, `columns`, `depends_on: frozenset[str]`.
- **`ResourceType`** — `MODEL`, `SOURCE`, `SEED`, `SNAPSHOT`, `OTHER` (catches tests, analyses, operations).
- **`Column`** — `name`, `data_type`, `description`.

The DAG lives in [`manifest/dag.py`](../../src/dblect/manifest/dag.py). `Dag.build(nodes, edges)` validates that every edge references a known node, detects cycles (raises `CycleError` with the witness cycle), and exposes `upstream(uid)`, `downstream(uid)`, `transitive_upstream(uid)`, `transitive_downstream(uid)`, and `topological_order()`. `Manifest.dag` materializes one from the project's `depends_on` graph, silently dropping edges to nodes the manifest didn't expose (e.g. upstream models from packages the project doesn't include).

Topological order is deterministic — ties are broken by node-id sort — so the audit walker iterates models in a stable order. Tests in `tests/manifest/test_dag.py` include hypothesis-generated acyclic DAGs to verify the order-respects-edges and transitive-closure invariants.

## The SQL layer (`src/dblect/sql/`)

Three modules:

- [**`parse.py`**](../../src/dblect/sql/parse.py) — Jinja redaction + sqlglot parsing.
- [**`patterns.py`**](../../src/dblect/sql/patterns.py) — list queries and detectors over the AST.
- [**`_sqlglot.py`**](../../src/dblect/sql/_sqlglot.py) — typed accessors over sqlglot's `Any`-heavy attribute surface.

### `ParsedSQL.parse(sql, dialect)`

dbt SQL is Jinja-laced, and sqlglot can't parse Jinja directly. The parser redacts Jinja in a way that keeps the surrounding SQL parseable:

- `{{ ref('x') }}` → bare identifier `x`. This makes joins and lineage read naturally to the AST walker.
- `{{ source('s', 't') }}` → `s__t` (compound sentinel).
- Other `{{ expr }}` → `__jinja_NNN` sentinel.
- `{# comment #}` and `{% statement %}` tags → stripped entirely.

**Redaction is line-preserving.** Every consumed newline is re-emitted so the redacted SQL has the same line count as the source. This invariant is what makes sqlglot's per-identifier line numbers (which we surface on findings) correspond to lines in the user's `.sql` file. A property-based test in `tests/sql/test_parse.py` exercises the invariant across composed Jinja inputs; targeted tests cover the multi-line-comment, multi-line-statement, and multi-line-expression shapes.

`ParsedSQL` is a frozen dataclass carrying `raw`, `redacted`, `dialect`, `tree` (sqlglot expression), and `placeholders: tuple[JinjaPlaceholder, ...]`. The `refs` property pulls model names from placeholder `ref(...)` calls in declaration order. `SQLParseError` is raised when the redacted SQL still won't parse (with the redacted text attached so users can see what sqlglot saw).

### Detectors and findings

`patterns.py` exposes six detectors, all pure functions over `ParsedSQL` returning `tuple[Finding, ...]`:

| Detector | What it flags |
| --- | --- |
| `detect_null_group_after_outer_join` | `LEFT/RIGHT/FULL JOIN ... GROUP BY <nullable-side-col>` — unmatched rows collapse into a NULL group. |
| `detect_coalesce_on_join_key` | `COALESCE(col, ...)` where `col` also appears in a JOIN's ON clause — silently masks "no match" vs. "match with NULL". |
| `detect_unordered_window` | Any of `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `PERCENT_RANK`, `CUME_DIST`, `NTILE`, `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`, `NTH_VALUE` over a window with no `ORDER BY`. |
| `detect_unordered_aggregate` | `ARRAY_AGG`/`STRING_AGG`/`GROUP_CONCAT` without `ORDER BY` or `WITHIN GROUP`. Element order across rows is undefined. |
| `detect_where_on_outer_joined_nullable` | `WHERE <nullable-side-col> = X` (or `!=`, `<`, `>`, `IN`, `BETWEEN`, `LIKE`) — silently inverts the OUTER JOIN to INNER. `IS [NOT] NULL` and `COALESCE(col, ...)` are protected. |
| `detect_non_deterministic_function` | `current_timestamp` / `now()` / `random()` / `uuid()` / etc. in load-bearing positions: JOIN ON, GROUP BY targets, window PARTITION BY, window ORDER BY. WHERE/HAVING are intentionally exempt (the incremental-lookback idiom). |

`scan_all(parsed)` runs them all and returns concatenated findings. `all_findings(parseds)` batches over an iterable.

Each detector emits one or more `Finding`s:

```python
@dataclass(frozen=True, slots=True)
class Finding:
    kind: FindingKind
    message: str
    sql_snippet: str
    line_start: int   # 1-indexed; 0 means "couldn't pin to a line"
    line_end: int
```

Line numbers come from `sg.line_range(node)`, which walks the node's `Identifier` descendants and takes min/max over each one's `meta["line"]`. sqlglot only stamps positions on identifiers, not on every expression — nodes with no identifier descendants (rare; literal-only expressions) report `(0, 0)`.

The detectors also expose **list queries** (`list_joins`, `list_windows`, `list_group_bys`, `list_aggregations`) that return dblect-shaped summary value types. Consumers don't need to import sqlglot to read structural facts about a statement.

`FindingKind` is a `StrEnum` so its values double as the JSON kind strings and the per-kind suppression names. There's a seventh kind, `MALFORMED_SUPPRESSION`, emitted by the suppression layer rather than a detector — see below.

## The audit layer (`src/dblect/audit/`)

Three modules:

- [**`walker.py`**](../../src/dblect/audit/walker.py) — `run_audit(manifest)` iterates models, runs detectors, applies suppression.
- [**`suppress.py`**](../../src/dblect/audit/suppress.py) — parses `-- noqa-fixture:` directives, matches them to findings.
- [**`reporter.py`**](../../src/dblect/audit/reporter.py) — `render_text` and `render_json`.

### Walker

`run_audit(manifest, *, detectors=DEFAULT_DETECTORS, dialect="duckdb") -> AuditReport`:

1. Iterates `manifest.models` in unique_id sort order for stable output.
2. For each model:
   - Skips if `raw_code is None` (sources, seeds, packages that don't ship SQL) — recorded as `SkippedModel(reason="no raw_code")`.
   - Calls `ParsedSQL.parse(raw_code, dialect)`. On `SQLParseError`, records `SkippedModel(reason="parse error: <details>")` and moves on. The walker **never raises on per-model failure** — one bad model shouldn't blind the audit to the rest.
   - Runs each detector, collecting `Finding`s.
   - Calls `parse_directives(raw_code)` to extract `-- noqa-fixture:` comments. Malformed comments (bare `-- noqa-fixture` with no reason) come back as their own findings of kind `MALFORMED_SUPPRESSION`.
   - Calls `apply(findings, directives)` to partition into active vs. suppressed.
3. Returns an `AuditReport` carrying `findings`, `suppressed`, `skipped`, and `models_scanned`.

Each active finding is wrapped in a `LocatedFinding(model_unique_id, file_path, finding)` so reporters can show file:line locations. Suppressed findings are wrapped in `SuppressedFinding(located, reason, directive_line)` which preserves both the original finding context and the directive that silenced it.

### Suppression

Syntax in SQL files:

- `-- noqa-fixture: <reason>` silences all kinds on the comment's line.
- `-- noqa-fixture: <FindingKind>: <reason>` silences only that kind. The leading token must be a known `FindingKind` value or it falls back to all-kinds (so typos and free-text reasons like `TODO: revisit Q3` don't silently fail to suppress).
- A directive applies on the same line as the offending SQL or the line immediately above it.
- A reason is required. A bare `-- noqa-fixture` produces a `MALFORMED_SUPPRESSION` finding so dangling directives are visible in PR review.
- Findings without line provenance (`line_start == 0`) are never suppressed.

`SuppressionDirective(line, kind, reason)` and `directive_matches(directive, finding)` and `apply(findings, directives)` are the building blocks; the walker glues them together.

### Reporters

**Text** ([`render_text`](../../src/dblect/audit/reporter.py)):

- Summary: `audit: scanned N models, M findings[, K suppressed][, L skipped]` (pluralized).
- Findings grouped by model (sorted by unique_id), within a model sorted by line. Each finding renders as `L{start}` or `L{start}-{end}`, the kind name, the wrapped message, and the snippet.
- Suppressed block (when populated): one line per silenced finding showing path, line range, kind, and reason.
- Skipped block (when populated): one line per skipped model with the reason.
- Findings with no line provenance render as `L?`.

**JSON** ([`render_json`](../../src/dblect/audit/reporter.py)):

```json
{
  "schema_version": "1",
  "summary": { "models_scanned": 5, "findings": 1, "suppressed": 0, "skipped": 0 },
  "findings":   [ /* flat finding objects with model_unique_id, file_path, kind, line_start, line_end, message, sql_snippet */ ],
  "suppressed": [ /* finding object + nested "suppression": { "reason": ..., "directive_line": ... } */ ],
  "skipped":    [ /* { "unique_id": ..., "reason": ... } */ ]
}
```

Keys are sorted for stable diffs. The `schema_version` field exists so consumers can branch on incompatible changes; bumps will be deliberate.

## The CLI (`src/dblect/cli/`)

A `typer.Typer` app registered as the `dblect` console script. Two commands today:

- **`dblect version`** — prints the installed version.
- **`dblect audit [PROJECT_DIR]`** — runs the audit.

Audit options:

| Option | Default | Notes |
| --- | --- | --- |
| `PROJECT_DIR` positional | `.` | Where `dbt_project.yml` lives. |
| `--manifest PATH` | _(unset)_ | Skip resolution and load this file directly. |
| `--dbt-executable NAME` | `dbt` | Used only by the fallback `dbt parse`. |
| `--format text\|json -f` | `text` | Reporter selection. Status messages stay on stderr only in text mode. |
| `--no-fail` | _(off)_ | Force exit 0 even when findings exist. Default is exit 1 on any unsuppressed finding. |

**Manifest resolution** (first wins):

1. `--manifest PATH` if provided.
2. `<project_dir>/target/manifest.json` if it exists.
3. Shell out to `dbt parse --project-dir <project_dir>` to produce one. Requires `dbt` on `PATH`; the error message tells the user when it isn't.

Each failure mode raises `typer.BadParameter` with an actionable message. The "no `dbt_project.yml` and no `--manifest`" case is caught explicitly so users don't get confusing dbt errors when they're just in the wrong directory.

## The execution harness (`src/dblect/execution/`)

[`run_model(project_dir, model_name, *, fixtures=None, ...) -> RunResult`](../../src/dblect/execution/run.py) copies a dbt project to a temp directory, optionally rewrites seeds with caller-supplied row dicts, runs `dbt seed` then `dbt run --select +<model>` against an ephemeral DuckDB file, and reads the produced table back through the DuckDB driver. Output rows come back in `RunResult` as `tuple[tuple[Any, ...], ...]` with column names alongside.

`RunError` carries `phase` (`"seed"` / `"run"` / `"query"`), exit code, stdout, stderr — so callers can branch on what failed without parsing dbt's output.

**This harness is not on the `dblect audit` path** today. It's the substrate the runtime-PBT and replay-determinism layers will sit on once those land. Right now it's exercised by `tests/execution/test_run.py` against the vendored jaffle fixture — confirming the harness works end-to-end so the runtime layer has something to build on.

## Tests

162 tests across the layers, organized by package:

```
tests/manifest/  - 25 tests, manifest parsing + DAG topology (incl. PBT)
tests/sql/       - 71 tests, parsing, Jinja redaction, detectors (incl. PBT)
tests/audit/     - 50 tests, walker, suppression, reporters
tests/cli/       -  9 tests, end-to-end CLI via typer.testing.CliRunner
tests/execution/ -  6 tests, real-dbt run against jaffle
tests/test_smoke.py - 2 tests, package import + CLI module load
```

Hypothesis property-based tests cover DAG topology, Jinja line-preservation, and round-trip parsing. The jaffle fixture (`tests/fixtures/`) carries a real `manifest.json` plus the underlying project, so detector tests can verify findings on actual dbt code rather than only synthetic SQL. End-to-end CLI tests use `typer.testing.CliRunner` to exercise the `dblect audit` flow against the same fixture.

Strict pyright and ruff in CI. The detectors and suppression module are pure functions, which makes the testing rigorous: the same inputs always give the same outputs, no mocking needed.

## What's deliberately not here yet

Forward-looking pieces that are designed but not built — see [docs/design/](../design/) for each:

- **Semantic types** and the typed-contract DSL (the doc set's "Tier 1" and "Tier 2").
- **Runtime checks**: replay-determinism via differential execution, heuristic invariants (row-count sanity, PK uniqueness, monotonicity), generator-driven structural PBT.
- **Generator framework**: contract-directed generation, intent catalog, multi-table coordinated generation.
- **Flag/var inference**: discovering dbt vars from SQL and scaffolding typed flag declarations.
- **`dblect init`**: scaffolds `dblect/`, generates editor stubs from the manifest, integrates with the project's package manager.
- **MCP server**, **HTML reports**, **SARIF output**, **YAML suppression config**.

Each of those is its own body of work. The current static analyzer is independent of them — it's the layer everything else builds on top of.
