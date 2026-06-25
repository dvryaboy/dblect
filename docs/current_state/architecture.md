# dblect: current architecture

A description of what's built right now, not what's planned. The forward-looking work that genuinely does not exist yet is the **runtime half**: property-based execution, replay-determinism via differential runs, the generator framework, multi-table coordinated generation, and the MCP server. Those live in [docs/design/](../design/). The typed-contract and semantic-types DSL described there *is* implemented as a static layer (the declaration family below); what is not yet built is running its contracts against generated data. For a capability-by-capability map of what works end to end today and the distance to a 0.1.0 release, see [capabilities.md](./capabilities.md).

What exists today is the **static analyzer**: one command (`dblect check`) that reads a dbt project and runs both detector families over it, the structural hazard detectors over every model's SQL and the declaration-level (domain-type and contract) checks where declarations exist, then prints findings the user can navigate to and act on. The structural family earns its keep against a real dbt project before any types or contracts are declared; the declaration family adds to it once declarations exist, and it is the layer that catches the headline meaning-shift bug (a column's revenue type flipping net to gross upstream) at PR-review time, statically, with no execution.

## What you can do with it today

```bash
$ dblect check path/to/dbt-project
dblect: 1 finding over 5 models (0 contracts resolved, 5 scanned, 0 predicate(s) collected)

coverage:
  resolution: 100.0% of columns (27/27)
  grounding: domain_type 0/27; functional_dependency 0/5
  contract columns checkable: 0/0
  worlds: 1 (base); no flag axes enumerated

structural findings:
  models/customers.sql  (model.jaffle_shop.customers)
    L44  null_group_after_outer_join
        GROUP BY orders.customer_id references column(s) from nullable
        join side (orders); unmatched rows collapse into a NULL group
        snippet: orders.customer_id
```

One command runs both detector families: the structural hazards (which need only the
compiled SQL, so they report on any project) and the declaration-level contracts under
`<project_dir>/dblect/` (which report zero contracts resolved when none are declared).
Exit code is `1` when unsuppressed findings exist (or `0` with `--no-fail`). `--format
json` produces a documented schema for CI consumption.

## Layout

```
src/dblect/
├── manifest/          # parse manifest.json into typed Node + DAG, surface dbt tests + constraints
├── adapters/          # per-warehouse AdapterProfile (dialect, write enforcement, hazardous builtins)
├── sql/               # parse compiled SQL, walk the AST, detect hazards
├── uniqueness/        # audit detectors (window order-keys, join fanout) over substrate keys
├── nullability/       # NULL-hazard detectors (group-by / join-key / NOT-IN over nullable upstreams)
├── snapshot/          # snapshot-model temporal-filter detector
├── lineage/           # lineage graphs + the facts substrate + one property propagator
├── types/             # the domain-type DSL: DomainType, refinements, scalars, fact bridge
├── contracts/         # ModelContract, @contract methods, column proxies, expression AST, stub generation
├── varinf/            # discover dbt var()/env_var() usage from source Jinja (flag-world scaffolding)
├── demo/              # the worked Money/Currency example domain used by walkthroughs and tests
├── audit/             # orchestrate the structural detectors over a manifest
├── check/             # resolve declarations, propagate domain-type + functional-dependency properties, report
├── analysis.py        # the one door: run both detector families, return one report
├── report.py          # render an AnalysisReport (both families) as text or JSON
├── cli/               # the `dblect` typer app
└── execution/         # run dbt models in DuckDB (used for execution tests; not on the check path)
```

Each package has a focused job and clean exports through its `__init__.py`. Internals live in `_*.py` modules.

## Data flow

The check pipeline (the structural family shown in full; the declaration family's
`run_check` propagation joins at the `analyze()` merge):

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
    │   - parse_directives() reads -- noqa comments from raw_code
    │   - apply() partitions into active / suppressed
    │
    ▼
AuditReport
    │   - findings: tuple[LocatedFinding, ...]
    │   - suppressed: tuple[SuppressedFinding, ...]
    │   - skipped: tuple[SkippedModel, ...]
    │
    │  (d) dblect.analysis.analyze() merges this with run_check()'s
    │      CheckReport into one AnalysisReport (both detector families)
    ▼
AnalysisReport
    │
    │  (e) dblect.report.render_text() or render_json()
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
- **`DbtTestMetadata`**: `name` (the generic-test name like `"unique"` or `"dbt_utils.unique_combination_of_columns"`), `kwargs` (heterogeneously shaped per test type), `namespace` (the package the test comes from, e.g. `"dbt_utils"`, or `None` for built-ins), plus the test-relevant slice of node config: `enabled` (defaults to `True`) and `where` (the row filter the test runs under, or `None`). The last two are pulled from the node's `config` block so downstream consumers can reason about test semantics from one place; the uniqueness and nullability discoverers skip disabled tests outright, and capture a `where`-filtered test as a *conditional* fact that carries the predicate but does not ground an unconditional annotation (see the conditional-facts note in the lineage section below).

The DAG lives in [`manifest/dag.py`](../../src/dblect/manifest/dag.py). `Dag.build(nodes, edges)` validates that every edge references a known node, detects cycles (raises `CycleError` with the witness cycle), and exposes `upstream(uid)`, `downstream(uid)`, `transitive_upstream(uid)`, `transitive_downstream(uid)`, and `topological_order()`. `Manifest.dag` materializes one from the project's `depends_on` graph, silently dropping edges to nodes the manifest didn't expose (e.g. upstream models from packages the project doesn't include).

Topological order is deterministic (ties are broken by node-id sort) so the audit walker iterates models in a stable order. Tests in `tests/manifest/test_dag.py` include hypothesis-generated acyclic DAGs to verify the order-respects-edges and transitive-closure invariants.

## The SQL layer (`src/dblect/sql/`)

Three modules:

- [**`parse.py`**](../../src/dblect/sql/parse.py): a thin wrapper that runs `sqlglot.parse_one` over the model's compiled SQL.
- [**`patterns.py`**](../../src/dblect/sql/patterns.py): list queries and detectors over the AST.
- [**`_sqlglot.py`**](../../src/dblect/sql/_sqlglot.py): typed accessors over sqlglot's `Any`-heavy attribute surface.

### The target adapter (`src/dblect/adapters/`)

A dbt project compiles against one adapter (duckdb, snowflake, bigquery, ...), and that single choice fixes everything dblect reasons about the target: which sqlglot dialect parses its compiled SQL, whether the warehouse enforces `PRIMARY KEY` / `UNIQUE` and `NOT NULL` on write, which incremental strategy runs when a model leaves `incremental_strategy` unset, and which builtin function names the non-determinism check treats as hazardous (the portable baseline plus the adapter's own, e.g. DuckDB's `txid_current()` / `nextval()`). [`AdapterProfile`](../../src/dblect/adapters/model.py) gathers those facets into one value so a run reads a single coherent target rather than assembling it from independent per-facet lookups. A dbt adapter name and a sqlglot dialect name are two namespaces that overlap by name without being the same thing; the profile carries both.

Profiles live behind a [registry](../../src/dblect/adapters/registry.py): each warehouse is a self-contained module under [`adapters/builtin/`](../../src/dblect/adapters/builtin/) that builds an `AdapterProfile` and calls `register(...)`. The registry auto-discovers those modules on first lookup, so **adding a warehouse is a new file and nothing else** (no central map to edit); an out-of-tree package extends support the same way, by calling `register` at import.

`profile_for_adapter(adapter_type)` is the semantics lookup: it returns the registered profile for a known adapter and a conservative profile for any other, never raising. `resolve_profile(adapter_type, explicit_dialect)` is the parsing-validation gate the CLI calls after loading the manifest. An explicit `--dialect` names the target wholesale, so its grammar and its runtime semantics always agree (forcing `snowflake` gives snowflake's dialect *and* snowflake's enforcement, never a hybrid); passing the flag is the operator's acknowledgment of a best-effort interpretation. Without it, the manifest's adapter must be validated, otherwise `UnvalidatedAdapterError` fires and the CLI bails with a message that names the adapter, the validated set, and the `--dialect` escape. When the resolved target is not validated, the CLI prints a one-line stderr warning so the run is never silently best-effort.

An adapter is **validated** when dblect's detectors have been exercised against its SQL end-to-end. Only `duckdb` is validated today (`validated_adapters()`).

The resolved `AdapterProfile` is threaded as the single target through `run_audit`, `run_check`, the detector makers, and the property constructors; the leaf parsing functions take the profile's `sqlglot_dialect`. Programmatic callers pass a profile directly (e.g. `profile_for_adapter("duckdb")`); the validation gate lives at the CLI boundary, not in the analysis pipeline.

### `parse_sql(sql, dialect)`

The analysis layer's input is compiled SQL — dbt has already rendered Jinja, so sqlglot sees real SQL with refs expanded and macros applied. `parse_sql` is a thin wrapper around `sqlglot.parse_one` that returns a sqlglot `Expr` and translates `sqlglot.errors.ParseError` into `SQLParseError`. `SQLParseError` carries the offending SQL on its `sql` attribute so the walker can record it on the skipped-model report.

### Detectors and findings

`patterns.py` exposes the structural detectors as pure functions over a sqlglot `Expr` returning `tuple[Finding, ...]`. The non-determinism check is adapter-bound (its hazardous-name set comes from the resolved `AdapterProfile`), so it ships as a `make_*` factory the audit builds per run:

| Detector | What it flags |
| --- | --- |
| `detect_null_group_after_outer_join` | `LEFT/RIGHT/FULL JOIN ... GROUP BY <nullable-side-col>`. Unmatched rows collapse into a NULL group. |
| `detect_coalesce_on_join_key` | `COALESCE(col, ...)` where `col` also appears in a JOIN's ON clause. Silently masks "no match" vs. "match with NULL". |
| `detect_unordered_window` | Any of `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `PERCENT_RANK`, `CUME_DIST`, `NTILE`, `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`, `NTH_VALUE` over a window with no `ORDER BY`. |
| `detect_unordered_aggregate` | `ARRAY_AGG`/`STRING_AGG`/`GROUP_CONCAT` without `ORDER BY` or `WITHIN GROUP`. Element order across rows is undefined. |
| `detect_where_on_outer_joined_nullable` | `WHERE <nullable-side-col> = X` (or `!=`, `<`, `>`, `IN`, `BETWEEN`, `LIKE`). Silently inverts the OUTER JOIN to INNER. `IS [NOT] NULL` and `COALESCE(col, ...)` are protected. |
| `make_non_determinism_detector(builtins)` | `current_timestamp` / `now()` / `random()` / `uuid()`, plus the resolved adapter's own builtins (e.g. DuckDB's `txid_current()`, `nextval()`), in load-bearing positions: JOIN ON, GROUP BY targets, window PARTITION BY, window ORDER BY. WHERE/HAVING are intentionally exempt (the incremental-lookback idiom). |

`scan_all(parsed, *, non_deterministic_builtins=...)` runs them all and returns concatenated findings (the non-determinism check uses the given builtins, defaulting to the portable baseline). `all_findings(parseds)` batches over an iterable. This static check is a fast pre-filter over a curated, necessarily incomplete list; the runtime replay-determinism loop is the completeness layer.

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

**A finding carries two spans.** `Finding.line_start`/`line_end` are the compiled-SQL span the parser saw (the raw observation). The walker then back-maps that span onto the on-disk `.sql` template via `audit/sourcemap.py`, and `LocatedFinding.located_span` reports the result: a `SourceSpan` whose `basis` is one of three: `SOURCE` when the construct aligns to a verbatim source line, `MACRO_CALL` when it lives in macro-generated SQL and back-maps to the single `{{ ... }}` call site that emitted it, and `COMPILED` when no source line could be found at all. The back-map aligns compiled and `raw_code` line-for-line on verbatim-passthrough content (whitespace collapsed, so a re-indent still matches). A line rewritten or emitted by compilation (a `ref()` becoming a relation name, a macro emitting a `group by`) does not match verbatim, so for those it reaches for the call site: the raw template's Jinja structure (read with the shared `templating` environment, no render) marks which source lines hold `{{ ... }}` constructs, and when the gap between two verbatim anchors holds exactly one such call site the emitted span anchors there. With several calls in that gap, or none to bound against, the span stays compiled-relative rather than being mapped to a guessed line. The text report marks a `MACRO_CALL` span `(via macro)` and a purely compiled one `(compiled)`; the JSON carries `source_line_start`/`source_line_end`/`line_basis` alongside the compiled `line_start`/`line_end`; SARIF places the region on any real source line (`SOURCE` or the `MACRO_CALL` site) and omits it for a purely compiled span, so a code-scanning UI never highlights a guessed line. Findings always carry the model's `original_file_path` either way.

`-- noqa` suppression matches on the same `located_span`. Directives live in `raw_code` and are read in source-line space, so matching them against the back-mapped span lets a directive on the line the report shows silence a finding whose compiled line a macro expansion shifted, and a directive on a `{{ ... }}` call line silence a finding that the macro emitted. A construct the back-map cannot anchor (several calls share its gap, or the model is fully generated with nothing to bound against) keeps a compiled-relative span, so it falls back to the compiled line. A model-or-kind-scoped directive would give those an always-expressible escape hatch; it is left for when the residual case is shown to bite in practice.

The detectors also expose **list queries** (`list_joins`, `list_windows`, `list_group_bys`, `list_aggregations`) that return dblect-shaped summary value types. Consumers don't need to import sqlglot to read structural facts about a statement.

`FindingKind` is a `StrEnum` so its values double as the JSON kind strings and the per-kind suppression codes. Each kind's suppression code is `DBLECT_` plus the value uppercased (`suppression_code()`), e.g. `DBLECT_JOIN_FANOUT`. A couple of its members are emitted from outside `patterns.py`: `NON_UNIQUE_WINDOW_ORDER_KEYS` + `JOIN_FANOUT` come from the fact-grounded detectors described below. The enum itself stays here so suppression codes and JSON consumers have one canonical list of kind strings.

## The uniqueness layer (`src/dblect/uniqueness/`)

Uniqueness is the first relation-scoped property on the facts substrate (see the lineage section below): a relation's value is its **candidate-key set**, grounded from declarations and inferred through the SQL by the relation reducer. The `uniqueness/` package is the audit-facing consumer of those keys: the window order-keys and join-fanout detectors plus the constructor that wires them.

[`detector.py`](../../src/dblect/uniqueness/detector.py) holds both detectors and `make_fact_grounded_detectors(manifest, *, dialect, parsed)`. The constructor runs one cross-model propagation of `uniqueness_property` over the relation graph, indexes the resulting candidate keys by relation name, and currys the detectors into plain `Detector` callables the walker drops into its pipeline. Each detector also consults a per-tree **scope index** (`relation_scope_keys`), the same relation walk applied to one parsed tree so a CTE's or inline subquery's keys are available; that index is cached so the walk runs at most once per tree.

The facts themselves come from declarations (`unique`, dbt-utils `unique_combination_of_columns`, native `PRIMARY KEY` / `UNIQUE`), from the `unique_key` of an incremental model whose `incremental_strategy` deduplicates on write (`merge` with a key, or `delete+insert`; an `append`-style or key-less strategy enforces nothing and grounds nothing, and the default strategy is read per adapter), and from the relation reducer's inference through `DISTINCT`, `GROUP BY`, `JOIN`, and `UNION`. The layer is **opportunistic by design**: it uses what the project gives it and stays silent everywhere else, because a warning on every undeclared model would be noise on projects that do not aggressively declare keys.

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

Both detectors are enabled by default. Because they're opportunistic, no opt-in flag is needed; projects without declared uniqueness simply see no findings from them. Findings of kinds `NON_UNIQUE_WINDOW_ORDER_KEYS` and `JOIN_FANOUT` are suppressible via the standard `-- noqa` syntax (a bare `-- noqa`, or `-- noqa: DBLECT_JOIN_FANOUT` for the one detector).

## The nullability layer (`src/dblect/nullability/`)

Nullability is a column-scoped property on the same substrate: which columns can carry a NULL, tracking outer-join introductions across the model graph. The discoverers ground it from declarations (`not_null` tests, native `NOT NULL` constraints) and the propagator taints columns that an outer join can leave unmatched. The detectors here consume that property to flag three hazards the structural pass cannot see without provenance:

- **`null_group_on_nullable_key`**: a `GROUP BY` over a column the lineage knows is nullable, so unmatched rows collapse into one NULL group. (The structural `null_group_after_outer_join` is the syntax-only sibling; this one fires on cross-model nullability.)
- **`join_on_nullable_key`**: a join whose key column is nullable, where NULLs never match and rows drop silently.
- **`not_in_nullable_subquery`**: `<col> NOT IN (SELECT <nullable> ...)`, the SQL footgun where a single NULL in the subquery makes the whole predicate return no rows.

Like the uniqueness detectors, these are opportunistic: with no grounded nullability they stay silent rather than guess.

## The snapshot detector (`src/dblect/snapshot/`)

dbt snapshots capture slowly-changing dimensions, and a snapshot whose query lacks a temporal filter re-captures unchanged history on every run. `snapshot_temporal_filter_missing` flags a snapshot model whose SQL has no filter on its updated-at / check column. The detector assembly lives in its own package so the audit walker stays agnostic to snapshot specifics.

## The declaration family (`src/dblect/types/`, `contracts/`, `check/`)

The declaration family is the static realization of the semantic-types and typed-contract DSL from [docs/design/](../design/). It is the half of the analyzer that catches meaning shifts rather than SQL-shape hazards, and it runs through the same lineage substrate as the structural family.

- [**`types/`**](../../src/dblect/types/): the domain-type DSL. `DomainType` is a Pydantic-shaped class whose fields are typed scalars (`Decimal(18, 2)`, enums, ...); `refine(...)` pins parameters to make a refinement (`Money.refine(currency=USD)`). `bridge.py` lowers a declared type into the facts the substrate grounds from.
- [**`contracts/`**](../../src/dblect/contracts/): `ModelContract` binds a domain type to a dbt model's columns; `@contract`-decorated methods build boolean expressions over column proxies (`proxy.py`, `ast.py`, `compile.py`) without importing sqlglot. `stubs.py` generates the editor `models` stubs `dblect init` writes.
- [**`check/`**](../../src/dblect/check/): `run_check` resolves the declarations against the manifest, propagates two properties over the substrate (a relation-scoped **functional-dependency** property, then a column-scoped **domain-type** property that reads it), and lets the findings fall out of what the substrate concluded: a declaration that does not resolve is a `CONTRACT_ISSUE`; a column whose inferred type contradicts its declared type is a `DOMAIN_TYPE_CONTRADICTION` (currency creep) reported wherever the taint reached; a reduction over one field of a multi-field type whose other fields are not held constant is an `AGGREGATION_NOT_WELL_TYPED` (the mixed-currency sum). `coverage.py` reports resolution and grounding so a clean report cannot hide thin coverage; `RESOLUTION_BELOW_FLOOR` fires when lineage resolves too little of the project.

Contract predicates (the `@contract` method bodies) are **collected and counted, not executed** today: running them needs materialized data, which belongs to the runtime loop. The static check stays static, so this family verifies type *propagation* and *coherence*, not value-level contract satisfaction.

`varinf/` sits alongside as a discovery pass: it walks source Jinja for `var()` / `env_var()` usage and builds a typed environment. It is the scaffolding the flag-world layer will enumerate over; it does not emit findings yet.

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

### Conditional facts

A dbt `unique` / `unique_combination_of_columns` / `not_null` test can carry a `where` filter: `unique(customer_id) where country = 'US'` asserts the key only over rows matching the predicate. The discoverers capture these rather than dropping them, emitting a `Fact` whose `condition` is the predicate (a `Predicate` in [`facts/model.py`](../../src/dblect/lineage/facts/model.py)). Grounding folds only *unconditional* facts into a scope's annotation, so a scope whose only fact is conditional grounds the IMPLICIT-top default exactly as if nothing were declared: the conditional claim stays captured and visible, never silently promoted to an unconditional one.

[`predicate.py`](../../src/dblect/lineage/predicate.py) is the property-agnostic engine that will decide when such a fact applies. `implies(strong, weak)` returns `True` only when it can prove every row satisfying `strong` also satisfies `weak`, within a totally-decidable fragment: conjunctions of `term <op> literal` and `term IN (...)` atoms, where a term is a column or a monotonic `date_trunc` bucketing of one, reasoned about by interval containment on the literals (so a narrower date bound implies a wider one, and a consumer that adds filters still implies each conjunct). Outside that fragment it returns `False` rather than guess, the same silent-when-unsure posture the rest of the audit takes. Soundness is the invariant that must not break, and property-based tests pin it by sampling concrete worlds across comparisons, `IN`, `OR`, truncation terms, and string ordering.

Activation itself (flowing each scope's accumulated row filter to the engine and promoting a matched conditional fact to a real annotation that uniqueness and nullability then consume) is the next increment and is not yet on the audit path.

### What the substrate proves today

`tests/lineage/test_pbt_lineage.py` generates dbt-shaped scenarios with sources, seeds, models, multi-upstream JOINs, repeated columns in projections (`a.x + a.x`), mixed-case identifiers, leaves with undocumented columns, and CTE-shaped models whose intermediates can combine multiple upstream columns. For every model output column the test compares the propagator's annotation to the leaf-level closure computed structurally from the scenario itself; the two must agree. A companion test pins that the recorded `edges` set lands on the immediate upstream relation (a leaf or the upstream model), with the propagator doing all transitive stitching. A pair of CTE-focused PBTs separately pin the single-source-intermediate and multi-source-intermediate cases so the CTE collapse path can't silently regress.

The where-provenance scenario tests in `tests/lineage/test_where_provenance.py` exercise pass-through, transform, aggregate, `COUNT(*)`, JOIN, and CTE collapse on small explicit SQL. A jaffle-fixture regression guard asserts that `build_manifest_graph` produces a non-empty graph and that per-column annotations agree with per-column edges on the real manifest. For uniqueness, `tests/lineage/test_uniqueness_propagation.py` pins the relation algebra end to end (passthrough, projection rename, JOIN coverage, GROUP BY, DISTINCT, UNION, cross-model) as the completeness anchor, and `tests/lineage/test_pbt_uniqueness_soundness.py` is an empirical soundness PBT whose oracle is execution rather than re-derivation: it generates dbt-shaped scenarios (filter, inner and left join, GROUP BY, DISTINCT, and where-filtered conditional activation), runs the analyzer to get each model's promoted keys, materialises the model against generated data in duckdb, and asserts every promoted key is genuinely unique over the rows. Completeness stays with the scenario examples; soundness (no over-claimed key) is the property the data oracle guards as new shapes are added. The uniqueness detectors consume these keys on the audit path today.

## The audit layer (`src/dblect/audit/`)

Two modules:

- [**`walker.py`**](../../src/dblect/audit/walker.py): `run_audit(manifest)` iterates models, runs detectors, applies suppression.
- [**`suppress.py`**](../../src/dblect/audit/suppress.py): parses SQLFluff-compatible `-- noqa` directives, matches them to findings.

Rendering is no longer audit-specific: the unified reporter ([`src/dblect/report.py`](../../src/dblect/report.py)) renders an `AnalysisReport` carrying both families. See **The unified report** below.

### Walker

`run_audit(manifest, *, detectors=DEFAULT_DETECTORS, dialect="duckdb") -> AuditReport`. A `Detector` is the type alias `Callable[[Expr], tuple[Finding, ...]]`; passing a custom list overrides the defaults (the fact-grounded detectors still run).

1. Pre-parses every model's `compiled_code` once and hands the trees to `make_fact_grounded_detectors(manifest, dialect=..., parsed=...)`, which propagates the uniqueness property over the relation graph once and curries the fact-grounded detectors against the resulting per-relation keys (reusing the same `Expr` per model rather than re-parsing). The curried detectors join the configured `detectors` list so the per-model loop runs everything in one pass.
2. Iterates `manifest.models` in unique_id sort order for stable output.
3. For each model:
   - Reads `Node.analysis_sql` (the model's `compiled_code`). Models with no compiled SQL are recorded as `SkippedModel(reason="no compiled SQL (run \`dbt compile\`)")`.
   - Calls `parse_sql(sql, dialect)`. On `SQLParseError`, records `SkippedModel(reason="parse error: <details>")` and moves on. The walker **never raises on per-model failure**: one bad model shouldn't blind the audit to the rest.
   - Runs each detector, collecting `Finding`s.
   - Calls `parse_directives(node.raw_code)` to extract `-- noqa` comments — directives live in the source the developer wrote, not in the compiled output, so they always come from `raw_code`.
   - Calls `apply(findings, directives)` to partition into active vs. suppressed.
4. Returns an `AuditReport` carrying `findings`, `suppressed`, `skipped`, and `models_scanned`. Convenience properties `counts_by_kind` (a `Counter`-backed `Mapping[FindingKind, int]`) and `has_findings` are available for consumers that want the rolled-up view without re-iterating.

Each active finding is wrapped in a `LocatedFinding(model_unique_id, file_path, finding)` so reporters can show file:line locations. Suppressed findings are wrapped in `SuppressedFinding(located, directive_line, bare)` which preserves both the original finding context and the directive that silenced it (`bare` records whether it was a kind-less `-- noqa`).

### Suppression

dblect reads the SQLFluff `-- noqa` syntax, the same one dbt Fusion's `dbt lint` honors, so one comment can address both a lint rule and a dblect finding. Syntax in SQL files:

- `-- noqa` (bare, no codes) silences every dblect finding on the comment's line.
- `-- noqa: DBLECT_<KIND>` silences only that detector. The code is `DBLECT_` plus the `FindingKind` (or `CheckFindingKind`) value uppercased, e.g. `DBLECT_JOIN_FANOUT`. Codes that do not start with `DBLECT_` are real lint rule codes that `dbt lint` owns, so dblect ignores them; `-- noqa: RF01, DBLECT_JOIN_FANOUT` quiets the lint rule and our finding in one directive.
- A directive applies on the line immediately above the finding's span, or anywhere within the span itself (`finding.line_start - 1 <= directive.line <= finding.line_end`). For single-line findings that collapses to "same line or one line above"; for multi-line findings (windows or joins that span several lines) the directive can sit on any line of the span.
- Every suppression is recorded in the report's `suppressed:` section (with the directive line and whether it was bare), so a silenced finding stays visible in review. dblect no longer owns the noqa syntax, so a malformed directive is `dbt lint`'s to police rather than something dblect flags.
- Findings without line provenance (`line_start == 0`) are never suppressed.

`SuppressionDirective(line, kind, reason)` and `directive_matches(directive, finding)` and `apply(findings, directives)` are the building blocks; the walker glues them together.

### The unified report

The reporter ([`src/dblect/report.py`](../../src/dblect/report.py)) renders an `AnalysisReport` (both families) for a terminal or for CI. One summary, one coverage block, and one JSON schema, so a reader and a machine consumer each see every finding in one place. Each family keeps its natural rendering: the structural family is grouped by model and line-located with the offending snippet; the declaration family is located by model, column, and contract, where a line span would not make sense.

**Text** ([`render_text`](../../src/dblect/report.py)):

- Summary: `dblect: N findings over M models (C contracts resolved, S scanned, P predicate(s) collected)` (pluralized).
- Coverage block (resolution, grounding, worlds), so thin coverage cannot hide behind a short finding list.
- `structural findings:` block, grouped by model (sorted by unique_id), within a model sorted by line. Each renders as `L{start}` or `L{start}-{end}`, the kind name, the wrapped message, and the snippet. No line provenance renders as `L?`.
- `declaration findings:` block: each renders as the kind name, the model (and `.column` when present), the message, and the file path.
- Suppressed, skipped, load-issue, and `could not analyze` blocks when populated.

**JSON** ([`render_json`](../../src/dblect/report.py)):

```json
{
  "schema_version": "1",
  "summary": { "findings": 1, "structural": 1, "declaration": 0, "models_analyzed": 5, "models_scanned": 5, "contracts_resolved": 0, "predicates_collected": 0, "suppressed": 0, "skipped": 0, "load_issues": 0, "unbuilt": 0 },
  "coverage": { "resolution": { /* ... */ }, "grounding": { /* ... */ }, "worlds": { /* ... */ } },
  "findings":   [ /* each tagged "family": "structural" | "declaration"; locator fields not relevant to a family are null */ ],
  "suppressed": [ /* finding object + nested "suppression": { "reason": ..., "directive_line": ... } */ ],
  "skipped":    [ /* { "unique_id": ..., "reason": ... } */ ],
  "load_issues":[ /* { "module": ..., "message": ... } */ ],
  "unbuilt":    [ /* { "unique_id": ..., "reason": ... } */ ]
}
```

Keys are sorted for stable diffs. The `schema_version` field exists so consumers can branch on incompatible changes; bumps will be deliberate.

## The analysis door (`src/dblect/analysis.py`)

Two detector families surface findings: `run_check` (declaration-level, located by model/column/contract) and `run_audit` (SQL-structural, located by a span). [`analyze(manifest, profile)`](../../src/dblect/analysis.py) runs both and returns an `AnalysisReport` whose merged `findings` carry both families, plus each family's own report for the family-specific extras (coverage, suppression).

This is the one door for any consumer that needs every family's findings (the CLI, the incremental-worlds cross-world diff, the world axes to come), so a consumer never enumerates the families itself and cannot silently drop one. `AnalysisFinding` is a sealed union (`CheckFinding | LocatedFinding`); per-family handling uses `match` with `assert_never`, so adding a third family is a type error at every site rather than a quiet coverage gap. `cross_world_identity` lives here too: a finding's identity across two compilations of the same project, ignoring the message and line span that drift between compiled SQLs.

`analyze` is the only production caller of `run_check` and `run_audit`; the CLI and the incremental check both go through the door. The two remain the entry points to two distinct subsystems, each with its own contract beyond "produce findings": `run_audit` carries a configurable detector set and `-- noqa` suppression, `run_check` carries the resolution floor and coverage. Those contracts are what `tests/audit/` and `tests/check/` pin, and they hold whether or not `analyze` exists, so the two are real boundaries rather than alternative doors. `analyze` is the single door for any consumer that needs every family; the two subsystem entries are not re-exported as top-level `dblect` API, and their docstrings point a both-families consumer back to `analyze`.

## The CLI (`src/dblect/cli/`)

A `typer.Typer` app registered as the `dblect` console script. Commands today:

- **`dblect version`**: prints the installed version.
- **`dblect check [PROJECT_DIR]`**: runs both detector families over the project (via [the analysis door](#the-analysis-door-srcdblectanalysispy)) and renders the unified report.
- **`dblect init [PROJECT_DIR]`**: scaffolds the `dblect/` declaration tree and writes the model stubs.

Check options:

| Option | Default | Notes |
| --- | --- | --- |
| `PROJECT_DIR` positional | `.` | Where `dbt_project.yml` lives (and `dblect/` if contracts are declared). |
| `--manifest PATH` | _(unset)_ | Skip resolution and load this file directly. |
| `--dbt-executable NAME` | `dbt` | Used only by the fallback `dbt compile`. |
| `--format text\|json -f` | `text` | Reporter selection. Status messages always go to stderr; stdout is the report. |
| `--dialect NAME` | _(unset)_ | Force a sqlglot dialect, overriding the manifest's `adapter_type`. Required when the adapter is not in dblect's validated set; passing the flag is the operator's acknowledgment that detector behavior is best-effort. |
| `--catalog PATH` | _(beside manifest)_ | A `catalog.json` supplying seed/source columns so undocumented DAG leaves resolve. |
| `--resolution-floor FLOAT` | _(unset)_ | Minimum share (0..1) of column references lineage must resolve; below it a `RESOLUTION_BELOW_FLOOR` finding fires so thin coverage is not read as a clean bill. |
| `--no-fail` | _(off)_ | Force exit 0 even when findings exist. Default is exit 1 on any unsuppressed finding. |

**Manifest resolution** (first wins):

1. `--manifest PATH` if provided.
2. `<project_dir>/target/manifest.json` if it exists.
3. Shell out to `dbt compile --project-dir <project_dir>` to produce one. Requires `dbt` on `PATH` and a working profile (the same setup `dbt run` needs); the error message tells the user when it isn't.

Each failure mode raises `typer.BadParameter` with an actionable message. The "no `dbt_project.yml` and no `--manifest`" case is caught explicitly so users don't get confusing dbt errors when they're just in the wrong directory.

## The execution harness (`src/dblect/execution/`)

[`run_model(project_dir, model_name, *, fixtures=None, ...) -> RunResult`](../../src/dblect/execution/run.py) copies a dbt project to a temp directory, optionally rewrites seeds with caller-supplied row dicts, runs `dbt seed` then `dbt run --select +<model>` against an ephemeral DuckDB file, and reads the produced table back through the DuckDB driver. Output rows come back in `RunResult` as `tuple[tuple[Any, ...], ...]` with column names alongside.

`RunError` carries `phase` (`"seed"` / `"run"` / `"query"`), exit code, stdout, stderr, so callers can branch on what failed without parsing dbt's output.

**This harness is not on the `dblect check` path** today. It's the substrate the runtime-PBT and replay-determinism layers will sit on once those land. Right now it's exercised by `tests/execution/test_run.py` against the vendored jaffle fixture, confirming the harness works end-to-end so the runtime layer has something to build on.

## The incremental-worlds check (`src/dblect/check/incremental.py`)

A dbt incremental model compiles two ways: a full-refresh form and a steady-state form whose `{% if is_incremental() %}` branch is present. A single manifest captures one, so a hazard in the unexercised branch is invisible. [`compile_incremental_worlds`](../../src/dblect/execution/incremental.py) produces both from `dbt compile` alone, data-free, by shadowing `is_incremental()` with a constant-returning macro and compiling once per value against an ephemeral DuckDB. Each world is read back as an ordinary `Manifest`.

[`check_incremental_worlds`](../../src/dblect/check/incremental.py) runs the project's detectors over each world through [the analysis door](#the-analysis-door-srcdblectanalysispy) (`analyze`, so both families are present by construction) and differences the per-world finding sets. Because these are control-flow worlds, the same issue renders with a different message and line span in each world, so the diff keys on the stable `cross_world_identity` that ignores those volatile parts. A finding holding in one world and not the other is the cross-world signal; one holding in both is what the single-manifest analysis already reports. The headline hazard it catches: a key the full-refresh build keeps unique is fanned out by a steady-state-only join, so the join-fan-out detector fires in steady-state alone. See [incremental-worlds.md](./incremental-worlds.md) for the macro-shadowing mechanism, the override's reach, and the planned refinements.

## Tests

The test suite is organized by package:

```
tests/manifest/    - manifest parsing + DAG topology (incl. PBT over generated acyclic DAGs)
tests/sql/         - sqlglot parsing wrapper, structural detectors (incl. PBT on parse round-trips)
tests/uniqueness/  - the window order-keys and join-fanout detectors over substrate keys
tests/lineage/     - lattice + semiring laws (PBT), where-provenance, nullability, uniqueness propagation, synthetic-DAG PBT incl. CTEs
tests/audit/       - walker, suppression directives
tests/check/       - contract resolution, world enumeration, incremental worlds + cross-world diff
tests/cli/         - end-to-end CLI via typer.testing.CliRunner
tests/execution/   - real-dbt run + incremental world-compiler against committed fixtures
tests/test_analysis.py - the analysis door surfaces both detector families
tests/test_report.py   - the unified reporter renders both families (text + JSON)
tests/test_smoke.py - package import + CLI module load
```

Hypothesis property-based tests cover DAG topology, parse round-trips, the lattice and semiring laws, the uniqueness JOIN-coverage and GROUP BY rules, and end-to-end lineage propagation on synthetic dbt-shaped DAGs (including CTE-collapse cases that previously dropped multi-source intermediates). The jaffle fixture (`tests/fixtures/`) carries a real `manifest.json` plus the underlying project, so detector tests can verify findings on actual dbt code rather than only synthetic SQL. End-to-end CLI tests use `typer.testing.CliRunner` to exercise the `dblect check` flow against the same fixture.

Strict pyright and ruff in CI. The detectors, uniqueness layer, and suppression module are pure functions, which makes testing rigorous: same inputs always give the same outputs, no mocking needed.

## What's deliberately not here yet

Forward-looking pieces that are designed but not built. The semantic-types DSL, the `@contract` declaration layer, `dblect init` with stub generation, and var/env_var discovery have all landed since the first draft of this doc; what remains unbuilt is the entire **runtime half** and the operator-facing tooling around it. See [docs/design/](../design/) for each, and [capabilities.md](./capabilities.md) for the full built-vs-unbuilt ledger:

- **Contract verification (execution)**: `dblect check` *collects and counts* contract predicates today but does not run them. Executing a contract needs materialized data, which is the runtime loop below.
- **Runtime checks**: replay-determinism via differential execution, heuristic invariants (row-count sanity, PK uniqueness, monotonicity), generator-driven structural PBT. The DuckDB execution harness exists but is not on the check path.
- **Generator framework**: contract-directed generation, intent catalog, multi-table coordinated generation. This is the largest single body of remaining work.
- **Flag/var worlds on the check path**: var/env_var *discovery* ships (`varinf/`), and fact-level flag-world plumbing is wired, but world enumeration and per-contract world scoping do not yet drive findings (issues #98–#100).
- **MCP server**, **HTML reports**, **SARIF output**, **YAML suppression config**, **change-impact / flag-flip preflight CLI**.

Each of those is its own body of work. The current static analyzer is independent of them. It's the layer everything else builds on top of.
