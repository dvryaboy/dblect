# dblect: capabilities and implementation

This document covers what dblect does, at each level of developer investment from zero declarations to focused contract chains, the mechanisms underneath, and the order in which the pieces should be built. It sits one level below the vision doc and one level above per-component design.

## Orientation

dblect operates at three layers of developer investment, each independently useful:

- **The audit** (no declarations): runs against the existing dbt project and reports real bugs. The day-one experience.
- **Semantic types on selected columns**: typed generators, cross-model tag tracking, type checking at model boundaries.
- **Focused contracts on critical chains**: compositional verification across a chain of dbt models, change-impact analysis at PR time, flag-flip preflight.

A team can stop at the audit and get value. A team can advance to typed columns on a single pipeline without committing to declare anything else. Focused contracts are opt-in per chain.

The mechanism underneath every layer is the same loop: parse dbt project structure, analyze SQL, generate coordinated test data, execute pipelines against generated data, check declared or inferred properties, shrink failures to minimal reproductions. The layers differ in what they declare, what they check, and what they catch.

---

## The audit

The user's interaction is one command:

```bash
dblect init
```

`dblect init` scaffolds the project (lays down `dblect/`, adds dblect to the project's dependency manifest, runs the package manager, generates editor stubs from the manifest), parses dbt, and runs the audit end-to-end. First findings land in under a minute on typical projects. Subsequent runs use `dblect audit` (re-runs the audit on the existing scaffolding) or `dblect check` (the contracts pipeline introduced once contracts are declared).

Internally, dblect does the following:

**Project ingestion.** Reads `dbt_project.yml`, parses `manifest.json`, builds the DAG of models, sources, and exposures. Maps every model to its compiled SQL.

**Per-model structural analysis.** For each model:
- Parse compiled SQL via sqlglot
- Identify source tables and join structure
- Identify aggregation patterns (GROUP BY, window functions, ROW_NUMBER, etc.)
- Identify ordering dependencies (ORDER BY, FIRST_VALUE, LAST_VALUE, ARRAY_AGG WITH ORDER, etc.)
- Identify non-determinism sources (`current_timestamp()`, `random()`, processing-order dependencies)

**Default generator construction.** For each source table, build a Hegel-backed generator from column types that respects schema constraints (types, `accepted_values` where declared, `not_null`, uniqueness), and FK relationships declared via dbt `relationships` tests.

**Structural PBT execution.** Via dbt-duckdb: generate small fixture datasets, materialize source tables in DuckDB, run the dbt model, capture output, run heuristic invariant checks against the output.

**Heuristic invariant checks at zero configuration:**
- Output row count not catastrophically different from input row count for non-aggregating models (fanout / drop detection)
- Identified PK columns are unique in output
- Inherently-positive aggregates (count, sum-of-positive) are non-negative
- Monotonicity of cumulative columns
- Conservation of identifiable quantities through obvious pass-through transforms

**Replay determinism via differential execution.** Run each model N times (default 5) on the same input, compare outputs under equivalence-aware diffing (multiset by default, order-up-to-ties when an explicit ORDER BY is present, set equivalence for set-aggregations). Flag models that produce *semantically* different outputs across runs, not just different row orderings.

**Static ambiguous-ordering detection.** For each `ORDER BY`, `ROW_NUMBER`, `FIRST_VALUE`, `LAG`, `LEAD`, `ARRAY_AGG ... ORDER BY`: check whether the order keys form a unique tuple over the relevant scope. If not, flag with the specific cause and a suggested fix (add a stable tiebreaker). For `ARRAY_AGG` / `GROUP_CONCAT` without `ORDER BY`: check downstream usage; if a downstream model takes `[0]` or `FIRST` or otherwise treats array order as semantic, flag the contract mismatch.

**Airflow task analysis (when Airflow is detected).** Run each task twice with identical inputs, compare outputs and downstream state, flag non-idempotence empirically. This is the seed of the Airflow-side capability; the audit keeps it empirical.

**Report generation.** HTML or markdown report with findings grouped by class and severity. Every finding includes location, description, a reproducer (the generated input that triggered it), and a suggested fix. Mute mechanism: `# noqa-fixture: <reason>` comment that suppresses with required justification, visible in PR review.

**What the audit catches:**
- Subtle SQL logic errors (wrong join conditions, NULL handling failures, ambiguous COALESCE)
- Replay non-determinism, particularly from ambiguous ordering rather than from clocks or random
- Join fanout
- Non-idempotent Airflow tasks
- Order-dependent downstream consumers reading from unordered upstreams

**What the audit doesn't catch:** meaning shifts, cross-model contract violations, flag-conditional bugs. Those require declarations.

---

## Semantic types on selected columns

The user declares semantic types in Python files under a `dblect/` directory in their dbt project:

```python
# dblect/types.py
from dblect import SemanticType
from dblect.types import Decimal

class Revenue(SemanticType):
    amount: Decimal(18, 2)
    currency: str
    contains_tax: bool
    contains_shipping: bool

RevenuePreTax  = Revenue.refine(contains_tax=False, contains_shipping=False)
RevenuePostTax = Revenue.refine(contains_tax=True,  contains_shipping=False)
```

And binds them to columns via a `ModelContract` class, Pydantic-shaped, with the dbt model identifier as a class attribute:

```python
# dblect/contracts/staging.py
import dblect
from dblect import ModelContract, Field, ForeignKey
import dblect.types as t

class StgOrders(ModelContract):
    dbt_model = "stg_orders"

    order_id:    t.PrimaryKey
    user_id:     ForeignKey("dim_users.user_id")
    revenue:     t.RevenuePreTax = Field(non_negative=True)
```

Flag-conditional refinement is declared via the `SemanticFlag` system (the canonical flag/type composition pattern, where the flag knows the type and declares its effect via `affects = RefinementEffect(...)`):

```python
# dblect/flags.py
from dblect import SemanticFlag, RefinementEffect
from .types import Revenue

class IncludeTaxInRevenue(SemanticFlag):
    """When set, revenue values include sales tax."""
    dbt_var = "include_tax_in_revenue"
    type = bool
    default = False
    affects = RefinementEffect(
        target=Revenue.contains_tax,
        value_when_true=True,
        value_when_false=False,
    )
```

See [flags_and_configs_as_types.md](flags_and_configs_as_types.md) for the full flag surface (`CompositeEffect`, `ConditionalEffect`, world enumeration).

Internally, dblect adds the following on top of the audit:

**Type-driven generators.** When generating test data for a column with a declared semantic type, use the type's generator method instead of the default. Generators carry constraints (positive, currency context, value ranges) and produce more realistic data. This catches bugs random data wouldn't trigger.

**Cross-model tag tracking.** When a downstream model references a typed column:
- Parse the SQL to determine if the column is passed through, transformed, or aggregated
- Propagate the type to the output column for pass-through usage
- Apply transformation rules where they exist (`column + tax_amount → adds with_tax`)
- Flag where type changes can't be inferred and require explicit declaration

**Boundary type checking.** At every model boundary where typed columns flow, check that the producer's output type matches the consumer's expected type. Flag mismatches with suggested resolution. For flag-conditional types, this check runs per-flag-world.

**Switch type resolution.** For flag-conditional types, at static analysis time:
- Enumerate live flag combinations (from the customer's declared flag environment)
- For each combination, resolve all types in the affected scope
- Check consistency in each "world"
- Report any world where types don't compose correctly

**Improved counterexamples.** Generated test data uses domain-aware types, so failures are reproduced with realistic-looking values. Pre-tax revenue values look like plausible pre-tax revenue, not random decimals.

**What semantic types catch additionally:**
- Type mismatches at model boundaries (consumer expects pre-tax, producer changed to post-tax)
- Flag-conditional inconsistencies (some flag worlds break, others don't)
- Arithmetic on typed columns that doesn't preserve the declared type
- Generator-revealed bugs that random data wouldn't trigger because it doesn't look real

---

## Focused contracts on critical chains

The user picks a target model whose correctness is critical, and declares the contracts the chain should satisfy:

```bash
dblect focus marts.fct_attributed_revenue
```

This is interactive. dblect proposes a minimum set of upstream declarations and the conservation, cardinality, and other contracts the chain should satisfy. The user reviews and accepts. Result lands in `dblect/contracts/`:

```python
from dblect import ModelContract, contract, models, Requires
import dblect.types as t

class FctAttributedRevenue(ModelContract):
    dbt_model = "marts.fct_attributed_revenue"

    conversion_id:       t.PrimaryKey
    event_date:          t.EventTime
    attributed_revenue:  t.RevenuePreTax

    requires_upstream = [
        Requires("stg_orders", "revenue", type=t.RevenuePreTax),
        Requires("dim_users",  "user_id", property="unique"),
    ]

    @contract.cardinality(relation="1:1", on="conversion_id")
    def one_row_per_conversion(self): ...

    @contract.conservation(tolerance=0.01)
    def attribution_conserves_revenue(self):
        return (
            self.attributed_revenue.sum().group_by(self.event_date)
            == models.stg_orders.revenue.sum().group_by(models.stg_orders.event_date)
        )

    @contract.replay_class("deterministic")
    def deterministic_given_inputs(self): ...

    @contract.late_data(tolerance_days=7)
    def tolerant_to_late_arrivals(self): ...
```

Contracts are decorated methods whose bodies build expressions over column proxies. `Requires(...)` entries declare the consumer's expectations on upstream columns. They act as a pressure mechanism for upstreams that haven't been typed yet, and a way to make semantic dependencies explicit where the expression body doesn't force them. Checks run statically (AST walk + type-registry lookup), no PBT needed.

Internally, dblect adds on top of the semantic-types layer:

**Backward inference.** Given a contract on a target model, walk the DAG backward and determine what each upstream model must satisfy. Generate proposed declarations for upstreams; the developer reviews and accepts. The `focus` interaction is largely automated drafting with human review.

**Compositional contract verification.** For each declared contract:
- Generate fixtures via coordinated multi-table generation (the harder data-gen story; see below)
- Execute the entire chain in DuckDB
- Check the contract holds
- Shrink failures to minimal counterexamples that preserve FK integrity

**Coordinated multi-table generation.** The engineering-intensive piece:
- State-machine-style generation that builds tables in dependency order
- FK-respecting (children reference real parent rows)
- Temporally coherent (events ordered correctly across tables; signup precedes purchase)
- Power-law cardinality distributions per FK relationship, parameterizable
- Hegel state-machine integration so shrinking respects relationships

**DAG propagation engine.** Properties (declared or inferred) propagate along edges via per-operator transfer rules. Forward propagation answers "given these source properties, what holds downstream?" Backward propagation answers "for this target contract to hold, what must upstreams satisfy?" Both are used.

**Change-impact analysis (the capability unique to focused contracts).** At PR time:
- Compute what changed in the PR: which models, which declared types, which contracts
- Walk the DAG forward to find affected downstream contracts
- Report: "this PR changes the type of `stg_orders.revenue` from `RevenuePreTax` to `Revenue(contains_tax=flag('include_tax'))`; downstream contracts referencing the old type: A, B, C"
- Gate the PR if affected contracts now fail; require explicit updates if they merely need adjustment

**Flag-flip preflight.** Before a flag is toggled (via admin UI, API, config change, env update):
- Simulate the post-flip world: which column types change, which contracts apply different definitions
- Report downstream impact across the chain
- Identify regime-spanning aggregations that would now mix worlds

**What focused contracts catch additionally:**
- Conservation violations across chains (attribution doesn't sum to source)
- Cardinality violations propagating through joins
- Late-data corruption affecting downstream invariants
- PR-time spooky-action-at-a-distance (locally innocent change breaks a contract three models away)
- Flag-flip downstream impact before the flag is flipped
- Regime-spanning aggregations that mix flag-world semantics

---

## Cross-cutting capabilities

A few capabilities apply across all layers rather than belonging to one:

**Static SQL analysis (via sqlglot).** Parsing, AST traversal, pattern recognition. Used by the audit for ambiguous-ordering and fanout detection; by the semantic-types layer for tag-propagation pattern recognition; by the focused-contracts layer for dependency tracking and change-impact propagation. One shared substrate; each layer consumes more of its output.

**Equivalence-aware diffing.** Outputs are compared under appropriate equivalence relations rather than byte-exact. Multiset by default. Order-up-to-ties when `ORDER BY` is present. Set equivalence for set-aggregations. Custom equivalences declarable per contract. Used everywhere outputs are compared (replay determinism, differential PR mode, contract verification).

**Counterexample shrinking and replay.** Every reported bug includes a minimal reproduction. Shrinking dimensions are domain-aware (minimum users, minimum days, minimum touchpoint types) rather than byte-level. `dblect show-case <id>` materializes the reproducer locally so the developer can run the bug interactively.

**MCP server.** Exposes dblect's analytical primitives as tools for LLM-environment integration: `read_dbt_manifest`, `analyze_model`, `propose_focus_chain`, `run_audit`, `check_contracts`, `generate_counterexample`. Lets Claude Code or any agentic environment drive declaration drafting, audit triage, contract setup. Independent of the CLI; same primitives, different interface.

**CLI.** Standalone CLI for headless and CI use. v1 verbs: `init` (bootstrap-to-first-findings, one shot), `audit` (re-run the audit), `check` (run contracts once they are declared, with `--flag-world` for selecting subsets), `show-case` (materialize a stored counterexample locally). `focus` (interactive contract drafting) and `impact --flag X` (flag-flip preflight) are slotted but deferred. Required for CI integration; sufficient for users who prefer not to drive via an LLM environment.

**Ignore mechanism.** Findings can be muted via `# noqa-fixture: <reason>` comments (the canonical syntax, same flavor as `# noqa: ...` in linters) or YAML config entries. Requires a reason; muted findings are reviewable in PR; mutes don't silently propagate.

**Persistence.** The framework maintains state across runs: catalog of declared types and contracts, counterexample library (reproducers from past failures, kept as regression tests), scenario template library (curated and customer-extensible).

---

## Implementation sequence

Order respects dependencies. Each milestone ends in a state where dblect does something usable end-to-end.

### Foundation

1. **dbt project ingestion.** Read `manifest.json`, build DAG, map models to compiled SQL.
2. **SQL static analysis layer (sqlglot wrapper).** Parse, traverse AST, identify common patterns (joins, aggregations, window functions, ordering, NULL handling).
3. **DuckDB execution harness.** Run dbt models in DuckDB against generated data, capture outputs reliably. Most effort here goes into dbt-duckdb adapter quirks.

### The audit

4. **Default generators from schema.** Type-driven generators from column types, respecting FK relationships derived from dbt `relationships` tests.
5. **Structural PBT runner.** Generate, materialize, execute, capture.
6. **Heuristic invariant checks.** The zero-config catalog (row-count sanity, PK uniqueness, non-negativity of inherently-positive aggregates, monotonicity of cumulative columns, conservation through obvious pass-throughs).
7. **Equivalence-aware diffing.** Multiset, order-up-to-ties, set equivalence. Becomes load-bearing in later phases.
8. **Replay determinism via differential execution.**
9. **Static ambiguous-ordering detection.** Pattern matching on the SQL AST.
10. **Report generation, ignore mechanism (`# noqa-fixture`), CLI basics (`init`, `audit`).**

**Milestone:** `dblect init` ships against real dbt projects, finds real bugs end-to-end.

### DSL and semantic types

11. **DSL implementation.** Semantic types as Python classes (scalar, B1 syntax), refinements, string-reference resolution. The single design-heaviest piece; budget for a throwaway first version.
12. **Type registry and resolution.** Flag registry, world enumeration, per-contract relevant-flag-subspace pruning.
13. **Type-driven generators.** Semantic types compile to Hypothesis generators with constraints.
14. **Cross-model tag tracking.** Type propagation through SQL pass-through via sqlglot column-level lineage; transformation rules for known operations; literals opaque-by-default with `dblect: preserves` / `dblect: discount(N)` / `dblect: tax(rate)` / `dblect: currency(from, to)` annotations.
15. **Boundary type checking.** Producer/consumer compatibility verification at every model boundary, per-flag-world.

**Milestone:** the semantic-types layer ships; users can declare types on critical columns and catch meaning shifts.

### Coordinated generation and intent catalog

16. **Multi-table coordinated generation.** State-machine-style with FK respect, intent-driven (v1-medium scope: pure synthesis, no mutation operators). The engineering crux.
17. **FK-aware shrinkers.** Custom shrinking on top of Hypothesis that preserves referential integrity.
18. **Temporal coherence in generation.** Event ordering across tables (signup precedes purchase, etc.).
19. **Intent catalog implementation.** Nine intents (Fanout, Orphan, NullKey, EmptyGroup, OrderingTie, ReplayShuffle, Duplicate, LateRow, Boundary), one per-intent spec + fixture-construction implementation + tests.
20. **Cardinality distributions.** Per FK relationship; parameterizable but defaults reasonable.

**Milestone:** intent-driven multi-table fixtures, ready for contract verification on focused chains.

### Focused contracts

21. **Contract DSL.** Conservation, cardinality, replay class, idempotence, late-data tolerance. Decorated-method form on `ModelContract` subclasses, expression bodies built over column proxies.
22. **Contract verification engine.** For each (contract, applicable-intent) pair: fixture generation, chain execution, contract checks, shrinking.
23. **DAG propagation engine.** Forward and backward propagation across the two lattices (structural + user-domain).
24. **`focus` command and contract drafting.** Interactive workflow, automated drafting with review. Deferred per [questions_and_decisions.md](questions_and_decisions.md); the slot is held for when demand surfaces.
25. **Change-impact analysis at PR time.** Delta computation, downstream contract impact, PR gating.

**Milestone:** full focused-contracts capability.

### MCP, polish, release

26. **MCP server.** Expose primitives for LLM-environment integration.
27. **Scenario template library and DSL.** First-party templates for common bug patterns, customer extensibility.
28. **Flag-flip preflight CLI** (`dblect impact --flag X`). Standalone command for operator workflows.
29. **Polish, docs, examples, real-project validation.**

**Milestone:** v1.0 OSS release.

### Deferred (post-v1)

- Airflow integration depth: task semantics declarations, retry semantics, mid-DAG restart fault injection. The empirical idempotence check from the audit covers the basics; deeper integration is its own project.
- Multi-warehouse fidelity layer: scheduled sampled runs against Snowflake / BigQuery to catch warehouse-specific edge cases the DuckDB execution misses.
- LLM-assisted declaration drafting: works via MCP-using environments out of the box; no built-in LLM workflow needed in v1.
- Mutation-based generation (v2-full): seed-mutation operators behind the intent catalog, with synthesis as fallback. Replaces v1-medium's synthesis-only path for realism.
- Demo walkthrough, MCP schemas, findings/SARIF format, counterexample persistence migration: figure out as they come up.

The longest single sub-task is multi-table coordinated generation with the intent catalog; it's where most of the implementation difficulty actually lives. The audit milestone is the shortest path to a shippable artifact and the earliest point at which the project provides verifiable value against a real dbt project. The audit, semantic-types, and focused-contracts milestones are the major capability landmarks; foundation, coordinated generation, and the release polish earn their keep by enabling the capability milestones.
