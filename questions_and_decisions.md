These are the places the four docs say different things. Ranked by how blocking they are for any demo work.

  1. Contract/model binding DSL. Three incompatible shapes appear.
  - dblect_technical_intro.md — class with dbt_model = "marts.fct_orders" attribute and decorated methods that build ASTs
  (@contract.conservation, @contract.cardinality).
  - tiers_and_rough_implementation_order.md — @dbt_model("stg_orders") and @dbt_contract("...") decorators on classes whose contracts are
  class attributes (cardinality = OneToOne(...), conserves = Conservation(...)).
  - The two styles produce very different ASTs, registries, and IDE experiences. Decorated-methods support multiple custom predicates per
  class; class-attributes are tidier for the common cases.

DESCISION: The option from dblet_technical_intro.md : Pydantic-shaped class where contracts are decorated methods whose bodies build symbolic expressions over column proxies.


  2. Flag/type composition direction. Three different stories about how a flag and a type connect.
  - Tech intro — type declares Revenue.switch(on=dblect.flag("X"), cases={...}). Type knows the flag.
  - Flags guide — separate class IncludeTaxInRevenue(SemanticFlag) with an affects = RefinementEffect(target=Revenue.contains_tax, ...)
  clause. Flag knows the type.
  - Tiers doc — Revenue.refine(contains_tax=sp.flag("...")). Inline flag reference inside refine.
  - The flags guide's "flag knows the type" direction is the most expressive (one flag can target multiple axes/types) and is the only one
  with the CompositeEffect/ConditionalEffect/OpaqueEffect story. The other two appear to be earlier iterations.


DECISION: yes, flags know the type. Flag guide is most current.


  3. Number of tiers. Design digest says four (Tier 3 = custom operator semantics for UDFs). Tiers doc says three. Pick one; the four-tier
  framing maps cleanly onto the three-tier doc by treating Tier 3 as deferred/expert-only.


DECISION: Yes, tier 3 is imaginary an deferred.


  4. SemanticType field shape.
  - Tech intro: class Revenue(SemanticType): base = dblect.types.Decimal; contains_tax: bool; ... — parameters as annotated fields; base is a
   class attribute.
  - Tiers doc: class Revenue(SemanticType): amount: PositiveDecimal; currency: Currency; contains_tax: bool; ... — amount/currency are fields
   alongside refinement axes.
  - These collapse two different conceptual moves: in the tech intro, Revenue is a refinement-bearing wrapper around a base Decimal; in
  tiers, Revenue is a record-shaped type with multiple fields. The downstream type-propagation rules differ.


DECISION: SemanticTypes are scalar. Each SemanticType wraps one SQL column: a base type (declared as a Pydantic-style annotated field, e.g. amount:   PositiveDecimal) plus zero or more refinement axes (annotated fields whose values are Python primitives or enums, e.g. contains_tax: bool, currency: Currency). Refinement axes are static metadata about the column, pinned by .refine(...) or varied by flags. Multi-column concepts (money-with-explicit-currency, address-with-parts, range-with-start-end) are modeled as separate columns linked by contracts rather than as record-shaped types. The rule of thumb: per-row varying values stay as their own columns and get cross-column contracts; values that are globally fixed for a model (or pinned by a flag) become refinement axes so the static type layer catches mismatches at PR time.


  5. Field-metadata namespace. sp.Field(source="...") and sp.flag(...) appear only in the tiers doc.
  dblect.ForeignKey("dim_customers.customer_id") appears only in the tech intro. Field(non_negative=True) appears in the tech intro. Pick one
   — probably dblect.Field, dblect.ForeignKey, dblect.flag.

DECISION: Yes, dblect.Field, dblect.ForeginKey, dblect.flag


  6. Generator framing. Tech intro and tiers doc describe Tier 2 generation as pure synthesis ("state-machine-style with FK respect").
  contract-directed-generation.md recommends mutation-as-default with synthesis fallback. The contract-directed doc is the latest and most
  considered, but the tiers doc's phase 4 ("Multi-table coordinated generation") doesn't reference mutation operators. Decide whether
  mutation is the default at Tier 2, or only at Tier 0's profile-overlay path.

DECISION: Initial version should ship intents + synthesis, no mutation. Intents drive what to generate; everything is synthesized. ~6–10 intents across the four contract categories. Realism is whatever type strategies produce — adequate for most contract verification, weak for distributional bugs. Skips the mutation-operator engineering. Demo has the "framework deliberately probed the fanout case" story. Leave intents + mutation + synthesis fallback + profile overlay for next round.
  
  7. Tier 0 scope ambiguity. Overview implies Tier 0 is "no declarations" and primarily static (it lists "ordering hazards,
  replay-determinism issues, foreign-key fanout risks"). Tiers doc has Tier 0 actually executing models via dbt-duckdb against synthesized
  data and running invariant checks. That's a real expansion. Not necessarily wrong, but worth confirming Tier 0 runs your dbt project, which
   is a meaningful install requirement.

DECISION   Tiers doc has it right.
  
  8. CLI command names. dblect flags scaffold (tech intro) vs dblect scaffold flags (flags guide). dblect init shows up only in tiers doc.
  Trivial to fix but pick a convention now.

DECISION `dblect init` should do as much work as possible. If we find the need for specific scaffold / flags commands we will add them at that point. 

  9. Ignore mechanism syntax. # dblect-ignore: <reason> (tiers) vs # noqa-fixture (contract-directed). Pick one.

DECISION: #noqa-fixture

  10. Switch type enumeration vs SemanticFlag worlds. Tech intro says "Switch flags are enumerated; the static analyzer enumerates flag
  worlds." Flags guide says "the framework enumerates every possible configuration of your flag values and propagates types through your SQL
  in each one." These are compatible, but the implementation surface differs: the tech intro's switch-type approach makes enumeration a
  property of the type; the flags-guide approach makes it a property of the flag registry. The right answer is probably "flag declarations
  drive a global world set; switch types are a convenience built on top." But that's a call to make explicitly.

DECISION flag guide is correct. switch type are an optional convenience if it doesn't come with a lot of code overhead.


  1. Which jaffle-shop project the demo runs on. Vanilla jaffle has no flags; the demo's flag-world story needs a fork. Decide: vanilla +
  planted flags, or jaffle-shop-classic, or a custom dblect-jaffle fork? This is the single biggest unblocking decision for any demo work.

DECISION: let's try to start with https://github.com/dbt-labs/jaffle_shop_duckdb and fork if we need to.

  2. Hegel's actual status. Both new docs treat it as real and shippable. Is it published, vendored, or vaporware? If it's not ready, v1 may
  need to sit on stock Hypothesis with a path forward to Hegel. Either way, get this on paper.

DECISION: https://hackage.haskell.org/package/hegel but we probably just end up using hypothesis . worth also looking at https://antithesis.com/blog/2026/hegel/ release announcement and the skills they package with it.

  3. Stdlib of built-in semantic types. The tiers doc casually uses PositiveDecimal, Currency, UniqueId, UserId from
  dblect.builtins/dblect.types. What's the v1 inventory? Even a one-pager listing them stops everyone reinventing.

DECISION: 
Stdlib of semantic types. Layered, mostly assembled from existing standards rather than invented.
  
  - SQL base types (Decimal, Integer, Date, Timestamp, Varchar, Boolean, Json, Uuid, …): re-exports of sqlglot's DataType with convenience
  constructors. No wrapper class. Generators and coercion rules live in free-function dispatch tables keyed by sqlglot type.
  - Constraint primitives (PositiveInt, NonNegativeDecimal, BoundedFloat, etc.): borrowed from annotated-types + Pydantic naming conventions.
   Used via Annotated[T, M], not via class inheritance.
  - String formats (Email, Url, UUID, Hostname, IpAddress): JSON Schema standard format names.
  - Refinement-axis enumerations (Currency, Country, LanguageTag): ISO 4217 / 3166 / BCP 47 value sets, shipped as enums.
  - Analytics primitives (Money, Identifier, PrimaryKey, ForeignKey[target], Count, Probability, Percentage, EventTime, LoadedAt, audit
  columns): hand-written SemanticType subclasses. Names and structure follow MetricFlow / DDD / dbt-utils precedent. This is where the real
  engineering goes.

  Pydantic is borrowed as a pattern (Field metadata, Annotated, class-shaped declarations), not as a base class — SemanticType and
  ModelContract use their own metaclass to avoid Pydantic's value-validation machinery. dbt's manifest data_type strings get parsed via
  sqlglot at load time; nothing to wrap.

  Deferred to later: addresses, geo, quantities-with-units, phone numbers, domain-specific tax/jurisdiction types. Users declare those in
  their own project until a clear stdlib case emerges.

  4. Concrete intent catalog. The generator doc promises "six to ten intents in v1" and names some by example. The full enumerated catalog
  (which intents for which contract category, with semantics) is still missing. Without it you can't build Phase 5.
 The v1 intent catalog

  1. Fanout(N). Generate one parent row and N child rows referencing it via the FK column the contract spans. Fixes the join key. Catches:
  conservation contracts that double-count because a downstream SUM multiplies across the fanout; cardinality contracts declared 1:1 that
  quietly become 1:N.
  
  2. Orphan(side). Generate rows on one side of a join with no match on the other. Parameterized by which side. Fixes the join key. Catches:
  inner-vs-outer join confusion, filter drift that drops legitimate rows, conservation gaps from one-sided records.

  3. NullKey(side). Generate rows with NULL on the join key. Fixes the join key column to NULL on a subset of rows. Catches: SQL's "NULL ≠
  NULL" semantics in joins and group-bys, COALESCE bugs, conservation failures from rows that silently drop.

  4. EmptyGroup. Generate a group key value in a dimension or upstream table that has no matching facts after filtering. Fixes the group_by
  column. Catches: aggregations that should return 0 but return NULL (or vice versa), missing-group handling in dashboards, conservation
  drift from groups appearing on one side but not the other.
  
  5. OrderingTie. Generate multiple rows that tie exactly on the ORDER BY columns used by ROW_NUMBER, FIRST_VALUE, LAG, LEAD, or ARRAY_AGG
  WITH ORDER. Fixes the ordering columns. Catches: deduplication via ROW_NUMBER() = 1 without a stable tiebreaker, non-deterministic "latest
  record" selection, semantic order in array aggregations.
  
  6. ReplayShuffle. Generate the same logical row set in a different physical arrival order across multiple runs. Fixes nothing about
  content; varies insertion/file order. Catches: processing-order-dependent logic, hash-based aggregation that drifts under reordering,
  replay-determinism contract violations.
  
  7. Duplicate. Generate an exact duplicate of an existing row (same business key, same payload). Fixes the duplicated row. Catches:
  idempotence violations in incremental models, dedup logic that silently fails, conservation that double-counts a logically-singular event.

  8. LateRow. Generate a row whose event_timestamp is earlier than already-processed data's watermark, then process it in a "current" batch.
  Fixes the event_timestamp column. Catches: late-data tolerance failures, incremental dbt models that miss back-dated events, aggregations
  that span partitions wrong.
  
  9. Boundary(bound). For cardinality contracts only — generate at and just over the declared bound (e.g., for "at most 1 per customer",
  generate groups of size 1 and size 2). Fixes the count-per-group. Catches: cardinality contracts that pass under typical data and fail at
  the edge.
  
  What each contract category gets

  ┌─────────────────────┬─────────────────────────────────────┐
  │  Contract category  │         Intents that apply          │
  ├─────────────────────┼─────────────────────────────────────┤
  │ Conservation        │ Fanout, Orphan, NullKey, EmptyGroup │
  ├─────────────────────┼─────────────────────────────────────┤
  │ Cardinality         │ Fanout, Orphan, Boundary            │
  ├─────────────────────┼─────────────────────────────────────┤
  │ Replay-determinism  │ OrderingTie, ReplayShuffle          │
  ├─────────────────────┼─────────────────────────────────────┤
  │ Idempotence         │ Duplicate, ReplayShuffle            │
  ├─────────────────────┼─────────────────────────────────────┤
  │ Late-data tolerance │ LateRow                             │
  └─────────────────────┴─────────────────────────────────────┘

  A typical model with 2–3 contracts ends up running 4–8 intent-driven test budgets, plus a happy-path baseline. The framework runs them in
  parallel; each one shrinks independently to a minimal counterexample if it fails.

  What's deferred to v2
  
  - Skew intents (power-law cardinality on join keys, hot-key concentration). Profile overlay covers some of this passively; deliberate skew
  injection is its own intent class.
  - Subpopulation intents (premium customers behave differently). Requires the user to declare a subpopulation predicate; not in v1.
  - Cycle intents (FK graphs with cycles, recursive references). v1 escape hatch is the noqa-fixture annotation; cycle-aware generation comes
   later.
  - Multi-step replay (incremental materialization as a state machine across N runs). Architectural support is there; the v1 build runs
  single-shot.
  - Mutation operators behind these intents. v1-medium generates from scratch via synthesis; v2-full adds mutation-from-seed so each intent
  can be reached either way.


  5. Findings/report schema. Tier 0 ships "HTML or markdown report." For CI integration you'll need JSON/SARIF-ish schema. Not blocking the
  demo if the demo is local, but blocking the CI story the overview promises.

lets figure it out as we go tbh

  6. What dblect init lays down. The recommended dblect/ skeleton is sketched in the tech intro but not specified as a template. dblect init
  is going to produce something — pin what.

  What init now does, end to end

  1. Detect dbt project — refuse helpfully if absent.
  2. Lay down scaffolding — dblect/ + .dblect/ + .gitignore + config :

  
  my_dbt_project/
  ├── dbt_project.yml              (unchanged)
  ├── models/                      (unchanged)
  ├── pyproject.toml               (adds [tool.dblect] section, or creates dblect.toml if no pyproject)
  ├── .gitignore                   (appends .dblect/ if not present)
  ├── dblect/                      (new, near-empty)
  │   ├── __init__.py              (empty; makes the directory importable)
  │   ├── types.py                 (docstring + commented example, no live code)
  │   └── contracts/
  │       ├── __init__.py          (empty)
  │       └── .gitkeep             (so the directory tracks)
  └── .dblect/                     (new, gitignored cache)
      └── .gitkeep

  3. Bootstrap project dependency — append dblect to the right dependency group in pyproject.toml / requirements-dev.txt / Poetry config.
  4. Install — detect the package manager (uv.lock → uv sync, poetry.lock → poetry install, otherwise pip install -e .[dev]) and run it.
  5. Parse dbt — invoke dbt parse to generate target/manifest.json. Use dbt-core if installed; fall back to dbt-artifacts-parser if only a
  stale manifest is present.
  6. Generate stubs — write dblect/_stubs/models.py from the manifest so future authoring gets autocomplete.
  7. Run the Tier 0 audit — static analysis (always), then execution-based checks via dbt-duckdb (graceful per-model degradation), then
  heuristic invariant checks.
  8. Write report — HTML in .dblect/audit-<timestamp>.html, summary in terminal.
  9. Print summary — findings count, top issues, link to full report, suggested next actions.

  Each step that can fail does so gracefully and the remaining steps run.

  Example output
  
  $ dblect init
  [dblect] Detected dbt project: my_jaffle_project
  [dblect] Created dblect/ with __init__.py, types.py, contracts/
  [dblect] Created .dblect/ cache directory
  [dblect] Added .dblect/ to .gitignore
  [dblect] Added [tool.dblect] to pyproject.toml
  [dblect] Added dblect to [dependency-groups.dev]

  [dblect] Installing project dependencies (uv sync)... done in 4.2s
  [dblect] Parsing dbt project (dbt parse)... done in 1.8s
  [dblect] Generated stubs for 22 dbt models → dblect/_stubs/models.py

  [dblect] Running Tier 0 audit:
    ✓ Static SQL analysis            22 models      0.6s
    ✓ Ambiguous-ordering detection   22 models      0.3s
    ✓ Replay determinism             22/22 ran      8.1s
    ✓ Heuristic invariants           20/22 ran     11.4s    (2 skipped: see report)
    ⊘ Airflow task analysis          no Airflow detected

  [dblect] Found 3 issues  (1 high, 2 medium)

    HIGH    marts/fct_orders.sql:42
            ROW_NUMBER() = 1 with non-unique ORDER BY columns
            Suggested: add a stable tiebreaker (e.g., order_id)

    MEDIUM  staging/stg_payments.sql:18
            COALESCE shadows NULL semantics in a join key

    MEDIUM  marts/dim_customers.sql:73
            ORDER BY clause not unique under partition
  
  [dblect] Full report:  .dblect/audit-2026-05-20-101522.html

  [dblect] Next steps:
    • Re-run audit anytime:  dblect audit
    • Declare types:          edit dblect/types.py
    • Add contracts:          dblect focus <model>

  The "wow" moment is built into init: you went from cd jaffle to three real findings in under 30 seconds.

  Graceful degradation
  
  Every step that touches the outside world can fail. Init keeps going where possible:

  - Install fails (no internet, version conflict, package manager missing): scaffolding is done. Print the install command to run manually.
  Skip parse/stubs/audit; user re-runs init or runs dblect audit after fixing install.
  - dbt parse fails (no warehouse creds for dbt deps, syntax error in user SQL, missing seed): scaffolding done, deps installed. Print the
  parse error verbatim. User fixes dbt, then runs dblect audit.
  - Some models can't execute in DuckDB (vendor-specific SQL, time travel, JSON ops DuckDB doesn't support): the per-model checks degrade.
  Static analysis still runs across all models. Report shows "16/22 models executed; 6 skipped — see report for vendor-SQL details." Skipped
  models are not failures of dblect, just gaps in coverage.
  - Manifest is stale / absent: try dbt parse; if that fails too, run only the static slice (which needs manifest.json but not execution).
  - No dbt-duckdb available: skip execution-based checks entirely; static slice runs; report says "install dbt-duckdb to enable
  execution-based audits."

  The principle: init produces whatever findings it can. The terminal output names what ran and what didn't.

  7. Flag environment / world declaration. Tier 1 mentions "flag environment management." Where does the user declare which flag worlds
  matter (vs the full product)? dbt_project.yml's vars: defaults? A separate dblect/environments.py? Multiple named environments (dev, prod)?

This is comlpetely superceded by the flag type system; that is what "flag environment management" was loosely envisioning.

  Don't add a dblect/environments.py or any separate environment-object concept. The current pieces handle it:

  - Flag types declare domain. The world space falls out.
  - Flag class default field + dbt_project.yml vars: together cover defaults; mismatches are reported.
  - requires_flags on contracts prunes worlds per contract.
  - CLI flags on check select subsets at run time.
  - Live-vs-theoretical tagging is optional metadata on findings (e.g., "this world is live in prod" derived from the union of
  dbt_project.yml defaults).



  8. dblect focus transcript. Described as "interactive" with "automated drafting with human review." The actual interaction shape (prompts,
  accept/reject flow, what gets written where) is undefined. This is a marquee Tier 2 capability for the demo.

we don't need this to get started.

  9. Equivalence-aware diffing user surface. Tier 0 defaults are spelled out; how does the user override per-contract? Likely a equivalence=
  parameter on contract decorators; not shown.


  For row matching, an equivalence= parameter on contract decorators, accepting either a string (common cases) or an Equivalence object
  (expressive cases):

  # String form — Tier 0 vocabulary
  @contract.replay_class("deterministic", equivalence="multiset")
  @contract.replay_class("deterministic", equivalence="set")
  @contract.replay_class("deterministic", equivalence="ordered")
  @contract.replay_class("deterministic", equivalence="ordered_up_to_ties")

  # Object form — for cases the strings don't cover
  from dblect import Equivalence as Eq

  @contract.replay_class(
      "deterministic",
      equivalence=Eq.per_group(by="customer_id", within_group="multiset"),
  )

  @contract.replay_class(
      "deterministic",
      equivalence=Eq.custom(my_compare_fn),    # last resort
  )

  For value equality, stay in the expression API. Value-level fuzzy comparison is what .within() and friends are already for:

  @contract.conservation(tolerance=0.01)
  def order_total_matches_line_items(self):
      return (
          self.order_total.sum().group_by(self.order_id).within(0.01)
          == models.stg_order_items.subtotal.sum().group_by(...)
      )

  A tolerance= shorthand on the conservation decorator is fine sugar for the common "all numeric columns within ε" case. Per-column tolerance
   goes in the expression body. Custom value predicates use existing operator-overloaded comparison.

  How inference + override interact
  
  When the contract decorator has no equivalence=, the framework infers from the SQL (the Tier 0 rules). When it's explicit, the framework
  uses the user value and records the override in findings: "equivalence overridden to multiset (inferred default was ordered_up_to_ties)."
  Users can see what they're shadowing.
  
  The override is per-contract, not per-model and not global. Two contracts on the same model can use different equivalences for different
  comparisons.

  Catalog of named equivalences (v1)
  
  Small, finite, matches the Tier 0 vocabulary plus the obvious extensions:

  ┌──────────────────────┬───────────────────────────────────────────────────────────────────────────┐
  │         Name         │                                  Meaning                                  │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ "exact"              │ byte-equal, order-equal, multiplicity-equal                               │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ "ordered"            │ row sequence matters; multiplicity matters                                │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ "ordered_up_to_ties" │ row sequence matters except where ORDER BY allows ties                    │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ "multiset"           │ order doesn't matter; multiplicity matters (the default for most outputs) │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ "set"                │ neither order nor multiplicity matters                                    │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────────┤
  │ "per_group"          │ grouped equivalence; multiset within each group, configurable across      │
  └──────────────────────┴───────────────────────────────────────────────────────────────────────────┘
  
  That's six. The Equivalence object form handles anything beyond: Eq.custom(fn) is the escape hatch, same spirit as @contract.check —
  possible, visible, rare.

  What about Tier 0 (no contracts)?

  Tier 0 audit doesn't have contracts to attach equivalence= to. The framework runs with inferred defaults; if the user disagrees with a
  specific finding, they suppress it via the # dblect-ignore: <reason> mechanism. That's a different feature (per-finding suppression) and
  shouldn't be conflated with per-contract equivalence override.
  
  If a user has a model that's intentionally non-deterministic and they want to bake that decision in once, they declare a contract:

  class FctSampledOrders(ModelContract):
      dbt_model = "marts.fct_sampled_orders"
      @contract.replay_class("nondeterministic")
      def sampling_is_expected_to_vary(self): ...

  Declaring the contract reclassifies the model and the audit no longer flags non-determinism on it. Clean way to upgrade an ignore into a
  typed statement.


  10. Per-flag-world contract skip semantics. requires_flags says "this contract only applies under this flag world." What about contracts
  that need to hold across worlds (e.g., a reconciliation that intentionally spans both)? Edge case, but the flag-flip preflight story needs
  it.

this seems straightforward to solve once we get to this point

  11. requires_upstream enforcement model. Tiers doc introduces Requires("stg_orders", "revenue", type=t.RevenuePreTax). Is this a contract
  that fails the consumer, the producer, or both? When does it run — static, PBT, both?

  requires_upstream. A Requires(model, column, type=... | property=...) entry on a ModelContract declares an expectation the consumer has on
  an upstream column. Two uses: (1) when the upstream isn't annotated yet, the consumer's Requires becomes a pressure mechanism that surfaces
   the missing annotation as a finding, pointing the upstream's author at what to add; (2) when the consumer's expression body doesn't itself
   force a particular semantic type (e.g., .sum() works for any decimal), Requires makes the consumer's semantic dependency explicit and
  checkable. The check runs statically as an AST walk plus type-registry lookup — no PBT, no execution. Failures are reported on the consumer
   (the contract that declared the requirement) with a pointer at the upstream column and the missing or mismatched property; the suggested
  fix is usually to add the annotation upstream. The type= form is checked against the user-domain lattice; the property= form (e.g.,
  "unique", "not_null") is checked against the structural lattice and against existing dbt schema.yml tests.

  12. MCP server schemas. Tools enumerated (read_dbt_manifest, analyze_model, propose_focus_chain, run_audit, check_contracts,
  generate_counterexample). Input/output schemas not yet drafted. Not blocking for v1 demo if you defer the MCP story.

  lets tackle this later

  13. Window functions and UDFs in the demo. Both deferred to v1.x as type-erasing boundaries. Jaffle has window functions (e.g.,
  row_number() in customer_orders). Decide how the demo presents the erasure boundary — as a feature (clear annotation), or hidden (skipped
  silently with a warning).
  
  clear annotation
  
  14. Counterexample persistence keys vs contract renames. Generator doc keys by (contract_id, intent). If a contract is renamed or
  restructured, what happens to stored examples? Migration story, or accept they get re-discovered?

 figure out later

  15. Demo walkthrough doc itself. The thing that says "here's the dblect/ tree we wrote, here's the eight commands the demo runs, here's
  exactly what each prints, here's the bug we planted and how it surfaces." Still not written. This is the single doc that turns the design
  into a demo.

figure out later