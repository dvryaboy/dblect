# Contract-directed generation for dblect

*Status: working design notes. Captures current direction on the generator architecture and the shape of the v1 commitment. The v1 intent catalog is enumerated below; per-intent specs (input parameters, fixture-construction algorithm, expected pass/fail semantics, minimal shrink target) are the next artifact to write. Shrinking heuristics within each intent are still being settled.*

## Where this fits in dblect

dblect's value is spread across three layers: static analysis (ordering hazards, type propagation, change-impact at PR time), declared domain types (typecheck for data pipelines), and property-based verification against user-declared contracts. The first two do useful work without generating any data. The third is where the generator earns its keep.

The point worth being upfront about: the generator does not have to carry the entire differentiated-value story. It has to carry the contract-verification layer specifically. That is a more bounded problem than "build a great general-purpose data generator," and the architecture in this doc is calibrated to that scope.

## The problem we're actually trying to solve

Property-based testing for analytics tables is hard because the input space is enormous and structurally rich. A column has marginal distribution, but it also has cardinality relative to other columns, null patterns that correlate with other columns, foreign-key participation, temporal placement, tie behavior under any declared order, and skew. The space is too large for purely random search to find interesting counterexamples on a reasonable budget. This is the central worry about whether PBT against analytics pipelines pays off at all.

The way Hypothesis works for ordinary code is illustrative. It works because the input space is usually small and well-typed (an integer, a list of strings, a parsed AST), and shrinking guides you to a minimal counterexample within that space. Hypothesis's authors have refined this to a high art over a decade, and Hegel inherits that work directly. When the input space matches the assumption (small, well-typed, mostly independent), this approach is excellent.

Analytics tables stretch the assumption. The cardinality bugs, aggregation bugs, and window-function bugs we want to catch live in the relational and temporal structure across tables, not in any one column's distribution. A generator producing 1000 rows of type-valid uniform random data will surface null-handling oversights and trivial join fanouts, but dbt unit tests with hand-written fixtures already catch those faster and with better failure messages. The honest pitch for PBT here is one of developer leverage: the author writes a contract once and the framework searches the shape space, rather than the author having to think of each failure shape and write a fixture for it. To make that pitch credible, dblect's generator has to find the bugs example-based tests would catch if the author had thought to write them, and ideally a few they wouldn't have.

Several research lines have explored this territory and inform the approach below. SQLancer and SQLsmith generate adversarial SQL to stress database engines, and their multi-table generation techniques are excellent starting points for foreign-key-honoring construction. AFL-style coverage-directed fuzzing showed how a feedback loop between observed behavior and generation can navigate large input spaces by being deliberate about where to look. Pandera demonstrated that schema declarations can double as generator specifications. dblect's job is to pull these threads together for a more constrained problem and ship something useful in a v1 timeframe.

## The reframe: contracts are generator specifications

The conventional PBT framing treats the generator and the property as independent. You generate inputs from a strategy, then check whether the property holds. The generator doesn't know what the property cares about, so it has to be broadly capable across the entire input space. This is the right design for a general-purpose PBT library, where the property could be anything.

dblect has more information available. A user-declared contract is also a specification of where the contract can fail. A conservation contract `sum(attributed_revenue) per day == sum(orders.revenue) per day` describes a structural relationship between two models on a join key with a grouping dimension. The shapes that can break this contract are enumerable: a join fanout on the touchpoint side, an orphan order with no touchpoint, a null on the join key that gets handled differently by the two aggregations, a day boundary that splits a logical event, a tie in the join key resolved differently by the planner. Each of these is a generation target.

This is the reframe worth taking seriously: contracts narrow the generator's job from "produce realistic adversarial data" to "produce data that probes these specific failure modes for this specific contract." The generator gets to be narrow and targeted, in the same spirit that coverage-directed fuzzers are narrow toward branch coverage.

The practical consequence is that the realism budget shrinks. Generating data that matches production marginals across all columns is irrelevant for most contracts. What matters is the structural shape on the columns the contract touches. Everything else can be cheap default fill: type-valid values from a small palette, with realism a v2 quality concern rather than a v1 correctness concern.

## Architecture: contract-directed intents

The proposed generator architecture has three layers.

**Intents.** A small library of generator templates, each parameterized by the contract type and the columns and grain involved. Each intent produces a structural shape known to stress a contract category. The v1 catalog is nine intents (Fanout, Orphan, NullKey, EmptyGroup, OrderingTie, ReplayShuffle, Duplicate, LateRow, Boundary), enumerated in detail below. Each contract category lights up a subset of these: conservation contracts get Fanout/Orphan/NullKey/EmptyGroup, cardinality gets Fanout/Orphan/Boundary, and so on. The catalog is deliberately finite and small; bug classes that aren't captured by the v1 intents are explicitly deferred to v2 (see below).

**Fill.** Once an intent fixes the structural shape on the contract-relevant columns, the rest of the schema is filled in with cheap defaults respecting foreign keys and declared domain types. The fill layer uses a Hegel state-machine-style construction (the approach is right for foreign-key integrity by construction) but pulls values from small palettes rather than trying to match production distributions. The fill exists to make dbt run without complaining, and that is enough.

**Profile overlay (optional).** When a profile is available from production sampling, the fill palettes get swapped for profile-derived strategies. Cardinality distributions on join keys, null rates, and time ranges come from real data. The intents still drive the structural shape; the profile adjusts the residual distributions toward realism. This is the layer that lets the OSS zero-declaration case ship a credible default without making the user write generator code.

The composition runs intents over the contract's relevant columns, hands off the residual schema to the fill layer, and optionally pulls overlay distributions from a profile. Each (contract, intent) pair becomes a separate test budget, so the framework explores all the structural shapes for each contract rather than betting random search will find them.

## Mutation as a complementary path (deferred to v2)

A mutation-based generator was considered for the v1 default. The decision is to defer it to v2 and ship v1 on pure intent-driven synthesis. This subsection records the mutation option for the future build.

The mutation approach starts from real data: the user's seeds, a sampled slice of their warehouse, or a representative fixture. The framework applies schema-aware mutation operators that target the same structural shapes the intent catalog targets. Drop a parent row referenced by an FK. Duplicate it. Null out a join key. Introduce a tie in an ORDER BY column. Shift a timestamp across a partition boundary. Inject a late-arriving row. Swap a row's group assignment. Each operator produces a candidate dataset that retains realism from its starting point and gains adversarial structure from the mutation.

Three advantages make it attractive long-term. Mutation operators are easier to write than synthesizers, since each one is a small local transformation rather than a coordinated multi-table construction. Inverse mutations give a natural shrinker, since each step of the mutation chain is reversible. Realism is inherited from the starting data, which sidesteps the question of how to match production distributions.

The reasons for deferring to v2: mutation requires seed data (which not every project has), the operator catalog is its own engineering effort, and pure synthesis is sufficient for the intent-driven structural-shape verification that's v1's pitch. v2 will add mutation as the default and keep synthesis as the fallback for shapes mutation can't reach (e.g., late-arriving rows when the seed has none).

## Shrinking is per-contract

A general shrinker working in bytes or rows produces messages users have a hard time with. "Here is a counterexample with 47 rows across four tables, of which some combination trips the property" leaves the localization work to the user. The shrinker should be goal-directed by the contract.

A conservation contract wants to minimize the number of distinct entities involved (one customer if possible), the number of distinct groupings (one day), and the total magnitude (smallest values that still trip the contract). A cardinality contract wants to minimize toward the boundary it tests. An idempotence contract wants to minimize the number of operations in the replay sequence.

The proposed shape is that contracts declare their shrinking targets as part of the contract definition (which columns and grains to minimize first, in priority order), and the shrinker is constraint-aware on the foreign-key graph but goal-directed by the contract. Reasonable defaults are inferred from the contract body when the user doesn't specify, so the common case doesn't require extra declarations.

This means shrinking is not a single framework-level concern. It is a per-contract concern with framework-level machinery for foreign-key integrity and example storage. The framework provides invariant preservation (don't orphan children, don't violate declared types); the contract provides the minimization objective.

## Failing-example memory from day one

Hypothesis's example database is one of the highest-leverage features in PBT, and dblect should ship analog from the first release. The pattern is well-understood: when a property fails, store the minimal counterexample. On subsequent runs, replay stored examples first before exploring new shapes. This converts random PBT into something closer to a regression suite as the test history accumulates.

The dblect version stores counterexamples keyed by contract and intent rather than just by property identity, since the framework knows both. A counterexample for "conservation contract on attributed_revenue under one-to-many fanout intent" can be replayed even after the SQL changes, as long as the contract identity persists. This is more durable than property-identity keying and tracks the user's mental model better: contracts are stable, SQL changes.

Storage format follows Hypothesis serialization conventions wherever possible, with extensions for the multi-table case. Reusing the convention means tooling and intuition transfer.

## The v1 intent catalog

Nine intents. Each is a generation template that fixes shape on specific columns; the rest of the row gets cheap fill. The framework runs each contract against every intent that applies to it.

**1. Fanout(N).** Generate one parent row and N child rows referencing it via the FK column the contract spans. Fixes the join key. Catches: conservation contracts that double-count because a downstream SUM multiplies across the fanout; cardinality contracts declared 1:1 that quietly become 1:N.

**2. Orphan(side).** Generate rows on one side of a join with no match on the other. Parameterized by which side. Fixes the join key. Catches: inner-vs-outer join confusion, filter drift that drops legitimate rows, conservation gaps from one-sided records.

**3. NullKey(side).** Generate rows with NULL on the join key. Fixes the join key column to NULL on a subset of rows. Catches: SQL's "NULL ≠ NULL" semantics in joins and group-bys, COALESCE bugs, conservation failures from rows that silently drop.

**4. EmptyGroup.** Generate a group key value in a dimension or upstream table that has no matching facts after filtering. Fixes the group_by column. Catches: aggregations that should return 0 but return NULL (or vice versa), missing-group handling in dashboards, conservation drift from groups appearing on one side but not the other.

**5. OrderingTie.** Generate multiple rows that tie exactly on the ORDER BY columns used by ROW_NUMBER, FIRST_VALUE, LAG, LEAD, or ARRAY_AGG WITH ORDER. Fixes the ordering columns. Catches: deduplication via `ROW_NUMBER() = 1` without a stable tiebreaker, non-deterministic "latest record" selection, semantic order in array aggregations.

**6. ReplayShuffle.** Generate the same logical row set in a different physical arrival order across multiple runs. Fixes nothing about content; varies insertion/file order. Catches: processing-order-dependent logic, hash-based aggregation that drifts under reordering, replay-determinism contract violations.

**7. Duplicate.** Generate an exact duplicate of an existing row (same business key, same payload). Fixes the duplicated row. Catches: idempotence violations in incremental models, dedup logic that silently fails, conservation that double-counts a logically-singular event.

**8. LateRow.** Generate a row whose event_timestamp is earlier than already-processed data's watermark, then process it in a "current" batch. Fixes the event_timestamp column. Catches: late-data tolerance failures, incremental dbt models that miss back-dated events, aggregations that span partitions wrong.

**9. Boundary(bound).** For cardinality contracts only. Generate at and just over the declared bound (e.g., for "at most 1 per customer", generate groups of size 1 and size 2). Fixes the count-per-group. Catches: cardinality contracts that pass under typical data and fail at the edge.

### What each contract category gets

| Contract category | Intents that apply |
|---|---|
| Conservation | Fanout, Orphan, NullKey, EmptyGroup |
| Cardinality | Fanout, Orphan, Boundary |
| Replay-determinism | OrderingTie, ReplayShuffle |
| Idempotence | Duplicate, ReplayShuffle |
| Late-data tolerance | LateRow |

A typical model with 2–3 contracts ends up running 4–8 intent-driven test budgets, plus a happy-path baseline. The framework runs them in parallel; each one shrinks independently to a minimal counterexample if it fails.

## Static findings as intent seeds

The reframe above treats a contract as a generator specification. A static hazard finding is a second source of the same information, and it is often a sharper one. When the static layer flags `where_on_outer_joined_nullable`, `join_on_nullable_key`, or `null_group_on_nullable_key`, it has already localized the exact join or predicate, the exact columns involved, and (for the WHERE case) a concrete suggested fix. That is precisely the input an intent needs. A finding on an outer-joined nullable side selects the Orphan and NullKey intents on that specific join; a fanout finding selects Fanout; an ordering finding selects OrderingTie. The static finding picks the intent and pins its parameters, so the generator runs one targeted budget against one join rather than searching the shape space for a contract.

This complements the contract-directed path rather than replacing it. Contract-directed generation searches broadly for whatever shape breaks a declared property. Finding-seeded generation starts from a specific suspect the static analyzer already surfaced and confirms it. The two share the same intent catalog, fill layer, and example memory; they differ only in what supplies the generation target.

The finding-seeded path also carries the property in the zero-declaration audit case. Where contract-directed generation needs a user-declared contract to falsify, a hazard finding ships with an implicit property derived from its own suggested remediation. For `where_on_outer_joined_nullable` the property is a differential: run the model as written and again with the suggested guard applied, and compare the row sets. An outer join that the WHERE has quietly demoted to inner shows a non-empty delta under an Orphan witness; a join that was genuinely inner all along shows none. The delta is both the proof and the triage signal. A non-empty delta is an active defect with a concrete witness row; an empty delta today marks the finding as dormant, which is exactly the latent-versus-active distinction a reader otherwise has to reason out by hand. This is the same spirit as the validation-mode story of running samples against the real warehouse: the static layer proposes, and a small generated or sampled run disposes.

A practical sequencing note: this path reuses the v1 intent catalog and example memory directly, so it can ship as a thin adapter from finding kind to intent selection once the generator core lands, without waiting on the full contract DSL.

## What v1 commits to

The v1 scope on the generator side:

1. The nine intents enumerated above, each well-specified and tested.
2. Pure intent-driven synthesis as the generation path (v1-medium). The fill layer handles residual columns cheaply, and mutation is deferred to v2.
3. Foreign-key-honoring construction for the fill layer, derived automatically from dbt's `relationships` tests with no separate declaration needed.
4. Per-contract shrinking with goal-directed minimization and foreign-key integrity preservation.
5. Failing-example memory keyed by contract and intent, Hypothesis serialization conventions where they apply.
6. Profile overlay as an optional enhancement to the zero-declaration audit, derived from production sampling.

## What v2 defers

Each of the deferred items below is a real category of bug. The reason for deferring them is that each one represents an order of magnitude more engineering than the v1 scope, and shipping v1 with a contract-directed core and clearly-labeled gaps is a better commitment than a delayed v1 that tries to cover everything.

- **Mutation operators.** v1 generates from scratch via synthesis; v2-full adds mutation-from-seed so each intent can be reached either way. Realism inherited from seeds is the v2 win.
- **Skew intents.** Power-law cardinality on join keys, hot-key concentration. Profile overlay covers some of this passively; deliberate skew injection is its own intent class.
- **Subpopulation intents.** Premium customers behave differently. Requires the user to declare a subpopulation predicate; not in v1.
- **Cycle-aware FK construction.** FK graphs with cycles, recursive references. Most dbt projects don't have these, and the engineering cost is substantial. The v1 escape hatch is a `# noqa-fixture` annotation that lets the user write the custom generator.
- **Late-data and out-of-order arrival as first-class concerns** beyond the single-row LateRow intent. v1 ships LateRow; comprehensive late-data testing (multi-row backfills, watermark advancement) is a v2 concern.
- **Multi-step replay.** Incremental materialization as a state machine across N runs. The architecture supports this; the v1 build runs single-shot.

## Influences

The architecture pulls from several existing lines of work.

**Hypothesis and Hegel** provide the underlying PBT machinery. The state-machine fixture construction, the shrinking philosophy, the example database, and the generator-as-strategy mental model all come from here. dblect's contribution is the layer above: contract-directed intents that decide what to generate, with Hegel handling how to generate and how to shrink.

**Pandera** demonstrated that schema-as-strategy works for DataFrame-shaped data. The unification of declaration and generation is directly transferable, and dblect's domain types lean on the same pattern. Pandera's `@check` decorator pattern also informs how contracts attach to models.

**SQLancer and SQLsmith** are the closest prior art for multi-table adversarial generation. Their foreign-key-honoring construction techniques inform the fill layer, and their treatment of dialect-specific edge cases is instructive for the validation-mode story of running samples against the real warehouse periodically.

**AFL and coverage-directed fuzzing** are the architectural reference for the intent-driven approach. The shift from broad random generation toward generation aimed at known interesting shapes is the same shift AFL made for binary fuzzing, applied to the contract surface rather than to branch coverage.

**dbt's `relationships` tests and the manifest** are the source of truth for foreign-key structure. Users who have already declared their schema in dbt shouldn't have to re-declare it for dblect.
