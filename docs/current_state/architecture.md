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
├── manifest/          # parse manifest.json into typed Node + DAG, surface dbt tests + constraints
├── sql/               # parse compiled SQL, walk the AST, detect hazards
├── uniqueness/        # audit detectors (window order-keys, join fanout) over substrate keys
├── lineage/           # lineage graphs + the facts substrate + one property propagator
├── audit/             # orchestrate detectors over a manifest, render output
├── cli/               # the `dblect` typer app
└── execution/         # run dbt models in DuckDB (used for execution tests; not on the audit path)
```

Each package has a focused job and clean exports through its `__init__.py`. Internals live in `_*.py` modules.

## Data flow

The audit pipeline:

```
dbt project
    │
    │  (a) dbt compile / pre-built target/manifest.json
    ▼
manifest.json (on disk)
    │
    │  (b) dblect.manifest.Manifest.from_file()
    ▼
Manifest (typed)
    │   - Nodes keyed by unique_id (models, sources, seeds, snapshots, tests)
    │   - raw_code (on-disk template), compiled_code (rendered by dbt)
    │   - original_file_path, depends_on
    │   - Test nodes carry DbtTestMetadata + attached_node
    │   - Models + Columns carry ConstraintSpec lists
    │   - DAG built from depends_on edges
    │
    ▼
    │  (c.1) make_fact_grounded_detectors() over the lineage.facts substrate
    │       One cross-model propagation of the uniqueness property, BEFORE
    │       per-model detection:
    │       - build the relation graph from the parsed models
    │       - propagate uniqueness_property → per-relation candidate keys,
    │         grounded by unique / unique_combination / native PK+UNIQUE
    │         declarations and inferred through DISTINCT, GROUP BY, JOIN, UNION
    │       → Mapping[relation_name, frozenset[CandidateKey]]
    │
    │  (c.2) dblect.audit.run_audit()
    ▼
For each model:
    │   - Read compiled_code (skip the model if absent)
    │   - parse_sql(compiled_code) ────► sqlglot AST
    │   - DEFAULT_DETECTORS run over the AST
    │   - Uniqueness-aware detectors run with the precomputed keys (plus a
    │     per-tree scope index for the model's CTEs and subqueries)
    │   - parse_directives() reads -- noqa-fixture: comments from raw_code
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

Status messages (manifest path, `dbt compile` invocation, fallback notices) go to stderr in both text and JSON modes, so stdout stays a clean report that consumers can pipe into `jq` or capture to a file.

## The manifest layer (`src/dblect/manifest/`)

`Manifest.from_file(path)` loads a `manifest.json` and produces a frozen, typed view. Internally it leans on `dbt-artifacts-parser` for the version-aware parse (dbt's manifest schema churns; we don't track that ourselves) and then transforms the parser's pydantic models into dblect's own dataclasses.

Key types in [`manifest/parse.py`](../../src/dblect/manifest/parse.py):

- **`Manifest`**: `schema_version`, `adapter_type` (e.g. `"duckdb"`, `"snowflake"` — sourced from `metadata.adapter_type`), `nodes: Mapping[str, Node]`, plus `models` / `sources` / `seeds` / `snapshots` properties that filter by resource type.
- **`Node`**: `unique_id`, `name`, `resource_type`, `fqn`, `package_name`, `schema`, `raw_code`, `compiled_code`, `original_file_path`, `columns`, `depends_on: frozenset[str]`, `constraints`, `test_metadata`, `attached_node`. The last three carry dbt-specific information that the uniqueness layer consumes: `constraints` for native dbt 1.5+ constraints on models, `test_metadata` (a `DbtTestMetadata`) for the `name`+`kwargs` of generic tests, and `attached_node` for the model a test is attached to.
- **`ResourceType`**: `MODEL`, `SOURCE`, `SEED`, `SNAPSHOT`, `OTHER` (catches tests, analyses, operations).
- **`Column`**: `name`, `data_type`, `description`, `constraints`. Column-level native constraints surface here.
- **`ConstraintSpec`**: `type` (a `ConstraintType`), `columns` (model-level; empty for column-level constraints), `expression` (CHECK predicate text).
- **`ConstraintType`**: `PRIMARY_KEY`, `UNIQUE`, `NOT_NULL`, `CHECK`, `FOREIGN_KEY`, `OTHER`. `from_raw` is total: unrecognized vendor- or dialect-specific types fall into `OTHER` so the parse stays total.
- **`DbtTestMetadata`**: `name` (the generic-test name like `"unique"` or `"dbt_utils.unique_combination_of_columns"`), `kwargs` (heterogeneously shaped per test type), `namespace` (the package the test comes from, e.g. `"dbt_utils"`, or `None` for built-ins), plus the test-relevant slice of node config: `enabled` (defaults to `True`) and `where` (the row filter the test runs under, or `None`). The last two are pulled from the node's `config` block so downstream consumers can reason about test semantics from one place; the uniqueness layer in particular skips disabled and `where`-filtered tests because their assertions don't ground unconditional facts.

The DAG lives in [`manifest/dag.py`](../../src/dblect/manifest/dag.py). `Dag.build(nodes, edges)` validates that every edge references a known node, detects cycles (raises `CycleError` with the witness cycle), and exposes `upstream(uid)`, `downstream(uid)`, `transitive_upstream(uid)`, `transitive_downstream(uid)`, and `topological_order()`. `Manifest.dag` materializes one from the project's `depends_on` graph, silently dropping edges to nodes the manifest didn't expose (e.g. upstream models from packages the project doesn't include).

Topological order is deterministic (ties are broken by node-id sort) so the audit walker iterates models in a stable order. Tests in `tests/manifest/test_dag.py` include hypothesis-generated acyclic DAGs to verify the order-respects-edges and transitive-closure invariants.

## The SQL layer (`src/dblect/sql/`)

Four modules:

- [**`parse.py`**](../../src/dblect/sql/parse.py): a thin wrapper that runs `sqlglot.parse_one` over the model's compiled SQL.
- [**`patterns.py`**](../../src/dblect/sql/patterns.py): list queries and detectors over the AST.
- [**`dialects.py`**](../../src/dblect/sql/dialects.py): adapter -> sqlglot dialect mapping and the validated-set gate.
- [**`_sqlglot.py`**](../../src/dblect/sql/_sqlglot.py): typed accessors over sqlglot's `Any`-heavy attribute surface.

### Dialect resolution

`ADAPTER_TO_SQLGLOT_DIALECT` in `dialects.py` is the explicit set of dbt adapters whose detector behavior dblect has validated end-to-end. Today that's `{"duckdb": "duckdb"}`. `VALIDATED_DIALECTS` is derived from the mapping's values.

`resolve_dialect(adapter_type, explicit_dialect)` is what the CLI calls after loading the manifest. An explicit `--dialect` always wins (the flag itself is the operator's acknowledgment that detector behavior is best-effort). Without it, the adapter must be in the validated mapping; otherwise `UnvalidatedAdapterError` fires and the CLI bails with a message that names the adapter, the validated set, and the `--dialect` escape. When the resolved dialect is outside `VALIDATED_DIALECTS`, the CLI prints a one-line stderr warning so the run is never silently best-effort.

Programmatic callers of `run_audit(manifest, dialect=...)` are unaffected: they pass whatever dialect they want and the walker forwards it to `parse_sql`. The gate lives at the CLI boundary, not in the walker.

### `parse_sql(sql, dialect)`

The analysis layer's input is compiled SQL — dbt has already rendered Jinja, so sqlglot sees real SQL with refs expanded and macros applied. `parse_sql` is a thin wrapper around `sqlglot.parse_one` that returns a sqlglot `Expr` and translates `sqlglot.errors.ParseError` into `SQLParseError`. `SQLParseError` carries the offending SQL on its `sql` attribute so the walker can record it on the skipped-model report.

### Detectors and findings

`patterns.py` exposes the static detectors, all pure functions over a sqlglot `Expr` returning `tuple[Finding, ...]`:

| Detector | What it flags |
| --- | --- |
| `detect_null_group_after_outer_join` | `LEFT/RIGHT/FULL JOIN ... GROUP BY <nullable-side-col>`. Unmatched rows collapse into a NULL group. |
| `detect_coalesce_on_join_key` | `COALESCE(col, ...)` where `col` also appears in a JOIN's ON clause. Silently masks "no match" vs. "match with NULL". |
| `detect_unordered_window` | Any of `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `PERCENT_RANK`, `CUME_DIST`, `NTILE`, `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`, `NTH_VALUE` over a window with no `ORDER BY`. |
| `detect_unordered_aggregate` | `ARRAY_AGG`/`STRING_AGG`/`GROUP_CONCAT` without `ORDER BY` or `WITHIN GROUP`. Element order across rows is undefined. |
| `detect_where_on_outer_joined_nullable` | `WHERE <nullable-side-col> = X` (or `!=`, `<`, `>`, `IN`, `BETWEEN`, `LIKE`). Silently inverts the OUTER JOIN to INNER. `IS [NOT] NULL` and `COALESCE(col, ...)` are protected. |
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

Line numbers come from `sg.line_range(node)`, which walks the node's `Identifier` descendants and takes min/max over each one's `meta["line"]`. sqlglot only stamps positions on identifiers, not on every expression, so nodes with no identifier descendants (rare; literal-only expressions) report `(0, 0)`.

**Line numbers refer to the compiled SQL the parser saw**, not to the on-disk `.sql` file the developer wrote. For models that don't use macros (refs only), compiled and source line up; for macro-heavy models, the compiled output may differ. Findings always carry the model's `original_file_path`, so the user can open the source file from the report and locate the construct from there. A future work item is to back-map compiled line numbers to source positions using the manifest's `raw_code`.

The detectors also expose **list queries** (`list_joins`, `list_windows`, `list_group_bys`, `list_aggregations`) that return dblect-shaped summary value types. Consumers don't need to import sqlglot to read structural facts about a statement.

`FindingKind` is a `StrEnum` so its values double as the JSON kind strings and the per-kind suppression names. A few of its members are emitted from outside `patterns.py`: `MALFORMED_SUPPRESSION` is raised by the suppression layer when a `-- noqa-fixture:` comment lacks a reason, and `NON_UNIQUE_WINDOW_ORDER_KEYS` + `JOIN_FANOUT` come from the fact-grounded detectors described below. The enum itself stays here so suppression syntax and JSON consumers have one canonical list of kind strings.

## The uniqueness layer (`src/dblect/uniqueness/`)

Uniqueness is the first relation-scoped property on the facts substrate (see the lineage section below): a relation's value is its **candidate-key set**, grounded from declarations and inferred through the SQL by the relation reducer. The `uniqueness/` package is the audit-facing consumer of those keys: the window order-keys and join-fanout detectors plus the constructor that wires them.

[`detector.py`](../../src/dblect/uniqueness/detector.py) holds both detectors and `make_fact_grounded_detectors(manifest, *, dialect, parsed)`. The constructor runs one cross-model propagation of `uniqueness_property` over the relation graph, indexes the resulting candidate keys by relation name, and currys the detectors into plain `Detector` callables the walker drops into its pipeline. Each detector also consults a per-tree **scope index** (`relation_scope_keys`), the same relation walk applied to one parsed tree so a CTE's or inline subquery's keys are available; that index is cached so the walk runs at most once per tree.

The facts themselves come from declarations (`unique`, dbt-utils `unique_combination_of_columns`, native `PRIMARY KEY` / `UNIQUE`) and from the relation reducer's inference through `DISTINCT`, `GROUP BY`, `JOIN`, and `UNION`. The layer is **opportunistic by design**: it uses what the project gives it and stays silent everywhere else, because a warning on every undeclared model would be noise on projects that do not aggressively declare keys.

**`detect_non_unique_window_order_keys`** flags window functions where the combined `(PARTITION BY + ORDER BY)` columns aren't covered by a known key of the scope's source:

- A scope is checkable when its FROM resolves to a **single relation** (no joins) with known keys: a ref'd model, or an in-scope CTE whose keys the scope index carries. Multi-source scopes need column-level lineage and stay silent.
- The source must have **at least one candidate key**. With no grounding, we stay silent.
- A key whose columns are a **subset** of the window's key set counts as coverage. Any superkey of a key is still a key (e.g. `id` declared unique covers a `(id, ts)` ranking).
- Only **bare column** order/partition keys are reasoned about. `order by date_trunc(...)` and similar computed keys are skipped.

**`detect_join_fanout`** flags JOINs whose joined-in side has known keys, none covering the join's equality predicate. A JOIN multiplies rows when the joined-in side has duplicates on the join key; a key of the joined-in side within the join columns rules that out.

- Every SELECT is inspected (including JOINs inside CTEs).
- The joined-in side must resolve to known keys (a ref'd model, or an in-scope CTE via the scope index). With no keys, we stay silent.
- The ON predicate must be a **conjunction of equalities between bare columns**, exactly one of which is qualified by the joined-in side's alias. Disjunctions, function calls, and range comparisons are skipped conservatively.
- A key whose columns are a **subset** of the join's right-side equality columns counts as coverage (superkey logic, same as window-keys).
- `CROSS JOIN` is skipped (it's an explicit cartesian, not a fanout-by-accident).
- A JOIN target whose name is shadowed by a local CTE resolves to the CTE's keys, matching SQL's resolution rules.

Both detectors are enabled by default. Because they're opportunistic, no opt-in flag is needed; projects without declared uniqueness simply see no findings from them. Findings of kinds `NON_UNIQUE_WINDOW_ORDER_KEYS` and `JOIN_FANOUT` are suppressible via the standard `-- noqa-fixture:` syntax.

## The lineage substrate (`src/dblect/lineage/`)

Most SQL footguns only become visible when you know where a column's *values* came from. Does this `LEFT JOIN ... WHERE upstream NOT IN (...)` silently drop rows because some upstream value is NULL? Is the column you're ranking on actually unique in the source, or did a CTE reshuffle it? Did an aggregate flatten away a key the next window function expects? Answering any of these means walking back from an output column, through the model graph, to the real source columns its values originated in, and reasoning about what happened along the way.

The substrate is a lineage graph plus a single walker that propagates *properties* over it. A property is a question like "which source columns did this trace back to?", "could this be NULL?", or "is this still a key?". Every property plugs into the same walker; adding a new one is writing a small dataclass, not a new traversal. Where-provenance is the simplest, computed per column. Uniqueness is the first relation-scoped property: it carries each relation's candidate-key set and is what the uniqueness detectors consume, so the old single-source bail is now cross-model propagation. A NOT-IN-nullable-upstream detector and an aggregate-over-aggregated detector are each a further property over the same engine.

The way values combine at expression crosses (JOIN) and confluence (UNION ALL) has the same algebraic shape no matter what's being propagated: a commutative semiring. That's why one `propagate` function handles where-provenance, nullability, and uniqueness through one engine, dispatching only the per-scope reducer. The framework is from Green, Karvounarakis, and Tannen ("Provenance Semirings", PODS 2007); the aggregate extension is from Amsterdamer, Deutch, and Tannen ("Provenance for Aggregate Queries", PODS 2011). You don't need either paper to write a property: say what the value is at a leaf, how two values combine, and which operators or aggregates do something special.

### Pieces

- [**`graph.py`**](../../src/dblect/lineage/graph.py): the lineage graph. Per output column we store its *edges* (the upstream columns it directly draws from) and its *expression* (the sqlglot AST that built it). Column names are case-folded so cross-model lookups don't trip over `id` vs `ID`.
- [**`builder.py`**](../../src/dblect/lineage/builder.py): turns compiled SQL into the graph. Per model it calls sqlglot's lineage walker and stamps each `Column` in the projection expression with the real source columns it resolves to. CTE intermediates collapse here: a CTE column built from `a.x + a.y` stamps the outer reference with both leaves. The cross-model variant walks the manifest DAG and merges per-model graphs; per-model failures land as `BuildIssue` entries instead of blanking the whole build.
- [**`semiring.py`**](../../src/dblect/lineage/semiring.py): the small algebraic interface a property gets to assume. Two implementations ship: a Boolean reference (`or` for branch-confluence, `and` for cross) and a set-union one for where-provenance. The set-union variant has a quirk worth knowing: the empty set is both `zero` and `one`, so it doesn't satisfy `0 × x = 0` the way a strict semiring would. A docstring and a Hypothesis test pin this explicitly so a future cleanup doesn't quietly break it.
- [**`property.py`**](../../src/dblect/lineage/property.py): the one propagator. `propagate(graph, prop)` is a memoised grounded fixpoint over the lineage DAG: it grounds each subject's declared annotation, short-circuits a declared opt-out, reduces the subject's derivation to an inferred annotation, and reconciles the two. The scope-specific part is a *reducer* chosen by `prop.scope_kind`: `_column_reduce` walks a column's projection expression (MRO lookup lets one rule on `AggFunc` catch every aggregate subclass), and the relation reducer walks a model's relation algebra. Everything else (grounding, reconcile, the cycle guard, memoisation) is shared, so column- and relation-scoped properties run through one engine. Graphs expose a tiny `LineageView` protocol (`subjects`, `derivation`).
- [**`properties/where_provenance.py`**](../../src/dblect/lineage/properties/where_provenance.py): the first column property. Each leaf annotates itself with `{self}`; every operator and aggregate unions inputs. The annotation on each output column ends up being exactly the set of source columns whose values fed it. [`properties/nullability.py`](../../src/dblect/lineage/properties/nullability.py) and [`properties/uniqueness.py`](../../src/dblect/lineage/properties/uniqueness.py) are the manifest-backed properties: nullability is column-scoped, uniqueness is the first relation-scoped property (its value is a relation's candidate-key set, and its reducer is the relation-algebra walk the uniqueness detectors consume).

### What the substrate proves today

`tests/lineage/test_pbt_lineage.py` generates dbt-shaped scenarios with sources, seeds, models, multi-upstream JOINs, repeated columns in projections (`a.x + a.x`), mixed-case identifiers, leaves with undocumented columns, and CTE-shaped models whose intermediates can combine multiple upstream columns. For every model output column the test compares the propagator's annotation to the leaf-level closure computed structurally from the scenario itself; the two must agree. A companion test pins that the recorded `edges` set lands on the immediate upstream relation (a leaf or the upstream model), with the propagator doing all transitive stitching. A pair of CTE-focused PBTs separately pin the single-source-intermediate and multi-source-intermediate cases so the CTE collapse path can't silently regress.

The where-provenance scenario tests in `tests/lineage/test_where_provenance.py` exercise pass-through, transform, aggregate, `COUNT(*)`, JOIN, and CTE collapse on small explicit SQL. A jaffle-fixture regression guard asserts that `build_manifest_graph` produces a non-empty graph and that per-column annotations agree with per-column edges on the real manifest. For uniqueness, `tests/lineage/test_uniqueness_propagation.py` pins the relation algebra end to end (passthrough, projection rename, JOIN coverage, GROUP BY, DISTINCT, UNION, cross-model) and `tests/lineage/test_pbt_uniqueness.py` generalises the JOIN-coverage and GROUP BY rules under Hypothesis. The uniqueness detectors consume these keys on the audit path today.

## The audit layer (`src/dblect/audit/`)

Three modules:

- [**`walker.py`**](../../src/dblect/audit/walker.py): `run_audit(manifest)` iterates models, runs detectors, applies suppression.
- [**`suppress.py`**](../../src/dblect/audit/suppress.py): parses `-- noqa-fixture:` directives, matches them to findings.
- [**`reporter.py`**](../../src/dblect/audit/reporter.py): `render_text` and `render_json`.

### Walker

`run_audit(manifest, *, detectors=DEFAULT_DETECTORS, dialect="duckdb") -> AuditReport`. A `Detector` is the type alias `Callable[[Expr], tuple[Finding, ...]]`; passing a custom list overrides the defaults (the fact-grounded detectors still run).

1. Pre-parses every model's `compiled_code` once and hands the trees to `make_fact_grounded_detectors(manifest, dialect=..., parsed=...)`, which propagates the uniqueness property over the relation graph once and curries the fact-grounded detectors against the resulting per-relation keys (reusing the same `Expr` per model rather than re-parsing). The curried detectors join the configured `detectors` list so the per-model loop runs everything in one pass.
2. Iterates `manifest.models` in unique_id sort order for stable output.
3. For each model:
   - Reads `Node.analysis_sql` (the model's `compiled_code`). Models with no compiled SQL are recorded as `SkippedModel(reason="no compiled SQL (run \`dbt compile\`)")`.
   - Calls `parse_sql(sql, dialect)`. On `SQLParseError`, records `SkippedModel(reason="parse error: <details>")` and moves on. The walker **never raises on per-model failure**: one bad model shouldn't blind the audit to the rest.
   - Runs each detector, collecting `Finding`s.
   - Calls `parse_directives(node.raw_code)` to extract `-- noqa-fixture:` comments — directives live in the source the developer wrote, not in the compiled output, so they always come from `raw_code`. Malformed comments (bare `-- noqa-fixture` with no reason) come back as their own findings of kind `MALFORMED_SUPPRESSION`.
   - Calls `apply(findings, directives)` to partition into active vs. suppressed.
4. Returns an `AuditReport` carrying `findings`, `suppressed`, `skipped`, and `models_scanned`. Convenience properties `counts_by_kind` (a `Counter`-backed `Mapping[FindingKind, int]`) and `has_findings` are available for consumers that want the rolled-up view without re-iterating.

Each active finding is wrapped in a `LocatedFinding(model_unique_id, file_path, finding)` so reporters can show file:line locations. Suppressed findings are wrapped in `SuppressedFinding(located, reason, directive_line)` which preserves both the original finding context and the directive that silenced it.

### Suppression

Syntax in SQL files:

- `-- noqa-fixture: <reason>` silences all kinds on the comment's line.
- `-- noqa-fixture: <FindingKind>: <reason>` silences only that kind. The leading token must be a known `FindingKind` value or it falls back to all-kinds (so typos and free-text reasons like `TODO: revisit Q3` don't silently fail to suppress).
- A directive applies on the line immediately above the finding's span, or anywhere within the span itself (`finding.line_start - 1 <= directive.line <= finding.line_end`). For single-line findings that collapses to "same line or one line above"; for multi-line findings (windows or joins that span several lines) the directive can sit on any line of the span.
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

- **`dblect version`**: prints the installed version.
- **`dblect audit [PROJECT_DIR]`**: runs the audit.

Audit options:

| Option | Default | Notes |
| --- | --- | --- |
| `PROJECT_DIR` positional | `.` | Where `dbt_project.yml` lives. |
| `--manifest PATH` | _(unset)_ | Skip resolution and load this file directly. |
| `--dbt-executable NAME` | `dbt` | Used only by the fallback `dbt compile`. |
| `--format text\|json -f` | `text` | Reporter selection. Status messages always go to stderr; stdout is the report. |
| `--dialect NAME` | _(unset)_ | Force a sqlglot dialect, overriding the manifest's `adapter_type`. Required when the adapter is not in dblect's validated set; passing the flag is the operator's acknowledgment that detector behavior is best-effort. |
| `--no-fail` | _(off)_ | Force exit 0 even when findings exist. Default is exit 1 on any unsuppressed finding. |

**Manifest resolution** (first wins):

1. `--manifest PATH` if provided.
2. `<project_dir>/target/manifest.json` if it exists.
3. Shell out to `dbt compile --project-dir <project_dir>` to produce one. Requires `dbt` on `PATH` and a working profile (the same setup `dbt run` needs); the error message tells the user when it isn't.

Each failure mode raises `typer.BadParameter` with an actionable message. The "no `dbt_project.yml` and no `--manifest`" case is caught explicitly so users don't get confusing dbt errors when they're just in the wrong directory.

## The execution harness (`src/dblect/execution/`)

[`run_model(project_dir, model_name, *, fixtures=None, ...) -> RunResult`](../../src/dblect/execution/run.py) copies a dbt project to a temp directory, optionally rewrites seeds with caller-supplied row dicts, runs `dbt seed` then `dbt run --select +<model>` against an ephemeral DuckDB file, and reads the produced table back through the DuckDB driver. Output rows come back in `RunResult` as `tuple[tuple[Any, ...], ...]` with column names alongside.

`RunError` carries `phase` (`"seed"` / `"run"` / `"query"`), exit code, stdout, stderr, so callers can branch on what failed without parsing dbt's output.

**This harness is not on the `dblect audit` path** today. It's the substrate the runtime-PBT and replay-determinism layers will sit on once those land. Right now it's exercised by `tests/execution/test_run.py` against the vendored jaffle fixture, confirming the harness works end-to-end so the runtime layer has something to build on.

## Tests

The test suite is organized by package:

```
tests/manifest/    - manifest parsing + DAG topology (incl. PBT over generated acyclic DAGs)
tests/sql/         - sqlglot parsing wrapper, structural detectors (incl. PBT on parse round-trips)
tests/uniqueness/  - the window order-keys and join-fanout detectors over substrate keys
tests/lineage/     - lattice + semiring laws (PBT), where-provenance, nullability, uniqueness propagation, synthetic-DAG PBT incl. CTEs
tests/audit/       - walker, suppression directives, text + JSON reporters
tests/cli/         - end-to-end CLI via typer.testing.CliRunner
tests/execution/   - real-dbt run against the vendored jaffle project
tests/test_smoke.py - package import + CLI module load
```

Hypothesis property-based tests cover DAG topology, parse round-trips, the lattice and semiring laws, the uniqueness JOIN-coverage and GROUP BY rules, and end-to-end lineage propagation on synthetic dbt-shaped DAGs (including CTE-collapse cases that previously dropped multi-source intermediates). The jaffle fixture (`tests/fixtures/`) carries a real `manifest.json` plus the underlying project, so detector tests can verify findings on actual dbt code rather than only synthetic SQL. End-to-end CLI tests use `typer.testing.CliRunner` to exercise the `dblect audit` flow against the same fixture.

Strict pyright and ruff in CI. The detectors, uniqueness layer, and suppression module are pure functions, which makes testing rigorous: same inputs always give the same outputs, no mocking needed.

## What's deliberately not here yet

Forward-looking pieces that are designed but not built. See [docs/design/](../design/) for each:

- **Semantic types** and the typed-contract DSL (the semantic-types and focused-contracts layers in the design docs).
- **Runtime checks**: replay-determinism via differential execution, heuristic invariants (row-count sanity, PK uniqueness, monotonicity), generator-driven structural PBT.
- **Generator framework**: contract-directed generation, intent catalog, multi-table coordinated generation.
- **Flag/var inference**: discovering dbt vars from SQL and scaffolding typed flag declarations.
- **`dblect init`**: scaffolds `dblect/`, generates editor stubs from the manifest, integrates with the project's package manager.
- **MCP server**, **HTML reports**, **SARIF output**, **YAML suppression config**.

Each of those is its own body of work. The current static analyzer is independent of them. It's the layer everything else builds on top of.
