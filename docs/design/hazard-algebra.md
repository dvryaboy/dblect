# Hazard algebra: effects, consumer sensitivity, and guards

Status: the local layer of this design is implemented. The value-effect guard catalog (`sql/guards.py`), the two-axis aggregate registry (`sql/aggregates.py`), the in-query fan-out collapse guard, and the inner-flatten deflation detector are landed, resolving issues #139, #168, #169, #170, and #63 as cells in the one framing rather than as private patches. The cross-model cardinality property (the last item in the refactor plan) remains the follow-on. It assumes the propagation calculus in [`propagation-soundness.md`](./propagation-soundness.md), the nullability work in [`nullability-hazards.md`](./nullability-hazards.md), and the substrate in [`lineage-facts.md`](./lineage-facts.md).

Audience: anyone working on the outer-join, fan-out, and row-drop detectors.

## The pattern

A cluster of detectors lives around outer joins and their cardinality relatives (`UNNEST`, `CROSS`, lateral flatten): the structural ones in `sql/patterns.py` (`null_group_after_outer_join`, `where_on_outer_joined_nullable`, `coalesce_on_join_key`), the fact-grounded uniqueness one (`join_fanout`), and the fact-grounded nullability family (`null_group_on_nullable_key`, `join_on_nullable_key`, `not_in_nullable_subquery`).

They are instances of one shape. A row-shaping operation injects an **effect** into the rows that flow downstream. The effect is harmless until it reaches a **consumer** whose result the effect changes, and harmless again if a **guard** neutralises it on the way. Every detector in the cluster searches for the same configuration:

> an **effect**, reaching a **sensitive consumer**, with no **guard** on the path between them.

Naming the three vocabularies and grounding each one is the design. The detectors then stop re-deriving the same questions privately, the recurring false-positive idioms (the COALESCE-fallback case, the OR-sibling case, the projection-merge case) become single shared catalog entries that compose, and the gaps (the inner-`UNNEST` row drop) are missing cells rather than new features.

## Two effects

A row-shaping operation does two independent things to the rows it emits.

- **Value effect (NULL padding).** The optional side of an outer join pads its columns with NULL on unmatched rows. `LEFT` mints it on the right, `RIGHT` on the left, `FULL` on both. The `nullability` property tracks this.

- **Cardinality effect (row count).** The operation changes how many output rows a driving row produces. **Inflation** replaces one driving row with several (fan-out: a join whose target is not unique on the key, an `UNNEST` of a many-element array). **Deflation** replaces one driving row with none (an inner join on a NULL or unmatched key, an inner `UNNEST` of an empty or null array, a WHERE that rejects the unmatched side of an outer join).

The two are orthogonal. A `LEFT JOIN` to a unique key pads NULLs with no cardinality change; an `INNER JOIN` to a non-unique key inflates with no NULL. Each effect carries its own propagated value and its own sensitivity question. The value effect is `nullability`; the cardinality effect is the property this cluster still lacks.

A third effect, **determinism** (does the same input give the same output), rides the same shape and is already implemented this way: `make_non_determinism_detector` fires a non-deterministic call only in a "load-bearing" position, and `_load_bearing_scopes` is a consumer-sensitivity classifier for it. It is out of scope here, and it shows the abstraction generalises beyond the two effects this doc unifies.

## Consumer sensitivity is a predicate, not a list

Each effect defines a single boolean sensitivity predicate from its own algebra, and a consumer is sensitive exactly when the predicate says so. The predicate is binary, so the partition into sensitive and insensitive is mutually exclusive and exhaustive. The example lists below are evidence tables for which side of the boolean a given SQL operation lands on; they may be incomplete, and an unrecognised operation takes the safe default (sensitive, keep firing).

### Value effect: three-valued logic

We fire only where a NULL has a **set-level consequence**: it changes row membership or which bucket a row falls into. This is the NULL-sensitivity of SQL's three-valued logic (the SQL standard's treatment of unknown; Date on nulls), applied to the positions that move rows:

- Equality and ordered comparison (`=`, `<`, `IN`, `BETWEEN`, `LIKE`): NULL yields UNKNOWN and the row is filtered.
- `GROUP BY` key: NULL forms its own bucket.
- Join-key equality: NULL never matches, so the row drops or pads.
- `NOT IN (subquery)`: one NULL makes the predicate never true, the extreme case.

Plain value propagation (`a + b` is NULL when `a` is) produces a visible NULL value with no set-level effect, so it is out of scope by this definition. `COALESCE` and `IS [NOT] NULL` consume NULL deliberately and are guards.

### Cardinality inflation: multiset algebra

A consumer is **duplicate-sensitive** when its result differs between a multiset and that multiset with duplicates removed (multiset relational semantics; aggregates over a commutative monoid, idempotent or not). The predicate reads a single positive field, so there is one concept (`duplicate_sensitive`) rather than a sensitive/insensitive pair to keep straight. A `DISTINCT` on the input removes the duplicates before the fold, clearing it; otherwise the answer is the aggregate type's recorded fact:

```
duplicate_sensitive(agg) = false              if strips_duplicates(agg)   # DISTINCT
                         = registry fact       otherwise                   # SUM -> true, MAX -> false
```

The registry fact records the *outcome* (sensitive or not); idempotence of the combine is one reason an aggregate lands on the safe side (`MAX`, `BOOL_OR`), and built-in deduplication or stable selection are others (`APPROX_COUNT_DISTINCT`, `ANY_VALUE`).

- `SUM`, `COUNT`, `COUNT(*)`, `AVG`, `ARRAY_AGG`, `STRING_AGG`: multiplicity-dependent. Sensitive.
- `MAX`, `MIN`, `ANY_VALUE`, `BOOL_AND`/`BOOL_OR`, `BIT_AND`/`BIT_OR`: idempotent. Insensitive.
- `COUNT(DISTINCT x)`, `SUM(DISTINCT x)`: deduped first. Insensitive.
- Row passthrough to output, or a downstream join on the column: the multiplication carries. Sensitive.

`strips_duplicates` is the `DISTINCT` modifier, a property of the AST node rather than the function type (`count(distinct x)` is `exp.Count` wrapping `exp.Distinct`, the same wrapper that flips `sum(distinct x)`), so it is one node-level helper read across every bucket. `AVG` is insensitive only under uniform duplication; a fan-out duplicates non-uniformly, so it stays sensitive, which the allowlist-the-insensitive default gives for free.

Inflation duplicates the whole row, not just the joined-in columns. When a driving relation `D` fans out against a non-unique target `T`, the `D` columns are replicated across the copies, so `SUM(d.amount)` inflates to `k * amount` even though `amount` never came from `T`. This is the fan trap. The condition for clearing inflation is therefore:

> on every path from the fan-out to the model output, the multiplicity is collapsed before any duplicate-sensitive consumer reads a column tracing to the replicated side.

Aggregating a joined-in (`T`) column with `SUM` is usually the intended set aggregation; aggregating a replicated (`D`) column with `SUM` is the fan trap. The discriminator is `where_provenance` (which side a read traces to) joined with the side `join_fanout` already identifies as lacking a covering key (the replicated side). The collapse mechanisms that clear inflation are `GROUP BY` to a grain the fan-out does not break, explicit `DISTINCT`, and a `qualify`/`row_number() = 1` dedup. These are guards.

### Cardinality deflation

Deflation has no consumer axis: a dropped row is gone, so every downstream reader is affected. Its "consumer" is the model's row-preservation expectation, and the hazard is the drop itself, so the deflation detectors (`where_on_outer_joined_nullable`, inner `join_on_nullable_key`, the inner-`UNNEST` case) fire at the introduction site. A deflation is benign only when a guard clears it: the operation written in its row-preserving form (`LEFT JOIN ... ON TRUE`, the outer flatten).

### One aggregate registry

Multiplicity-sensitivity and magnitude-coherence are orthogonal facts about the same fold, recorded in one registry rather than two parallel tables. The magnitude axis (`sql/aggregates.py`, `AggregateBehavior`) asks what domain the result lives in: the fold returns one of its inputs (`SELECT`), synthesizes a new value (`COMBINE`), or leaves the domain for a cardinality (`COUNT`). The multiplicity axis asks whether the combine operation is idempotent (`x ⊕ x = x`). They agree on the common arithmetic aggregates (`MIN`/`MAX` are `SELECT` and idempotent, `SUM`/`AVG` are `COMBINE` and non-idempotent) and come apart where it matters: `bit_xor` synthesizes but is non-idempotent (sensitive), the boolean and bitwise-and/or folds are non-magnitude but idempotent (insensitive), and `COUNT` leaves the domain but splits on whether its input was deduped.

So the registry holds one entry per aggregate type with two orthogonal fields: `AggregateBehavior` derives from the result-domain field, and a `duplicate_sensitive` boolean from the multiplicity one. The boolean, bitwise, and collection folds the magnitude axis leaves unclassified gain a definite duplicate-sensitivity fact while their magnitude answer stays "no obligation."

The registry is type-keyed for dialect-neutral aggregates and extended by a name-keyed contribution the adapter declares (`AdapterProfile.duplicate_safe_aggregate_builtins`, the shape `non_deterministic_builtins` already uses), so a warehouse can name the duplicate-safe UDFs sqlglot leaves as `exp.Anonymous` (duckdb `product`, `geometric_mean`, `favg`, `mad`, and kin; the gap `aggregates.py` tracks as #119). The multiplicity default is sensitive, so an unclassified UDF keeps firing rather than silently clearing a fan-out.

## When to fire: anchor, then guard

A hazard is a chain of three gates, and we apply a different posture to each.

1. **The effect exists.** A firewall: fire only on a scarce positive fact that anchors the effect, never on a default. Proven `NULLABLE`, "a known key on the target is not covered by the join", and the inner form of an array flatten are anchors. "A column is nullable because SQL defaults nullable" is not. This gate holds the noise down.
2. **It reaches a sensitive consumer.** A positive, provable fact: a `SUM` reading the column sits in the AST or the lineage graph. Required as evidence, not assumed.
3. **The data realizes the harm.** Not statically knowable, and with no runtime probe we do not gate on it. Its absence is why a finding is framed as a likely hazard rather than a certain one.

Given an anchored effect reaching a sensitive consumer, the guard search is broad-net: fire unless a clearing guard or a provably-insensitive consumer is recognised. The evidence tables allowlist only proven-safe operations, so an incomplete table costs a false positive, never a false negative.

So the posture is split: a firewall on the anchor, a broad net on the clearing. Deflation collapses gate 2 into gate 1 (the anchor is the whole hazard). The three states:

| State | Condition | Action |
| --- | --- | --- |
| Likely hazard | anchor present, reaches a sensitive consumer, no recognised guard | fire |
| Provably safe | a guard clears the effect, or the consumer is provably insensitive | suppress |
| Pure ignorance | no anchor (no known key, no proven null) | silent |

The line between fire and silent is the anchor, the scarce fact that makes the hazard likely.

### Anchor strength feeds severity

The two effects anchor with different strength. The value-effect anchor is a proof (proven `NULLABLE`). The inflation anchor is a likelihood ("a known key is not covered by the join" says the modeler departed from a grain, but we cannot prove non-uniqueness of the join column). Both fire under the gate above; the difference is severity, not a second fire decision. Severity is a function of anchor strength and consumer kind (a `SUM` of a replicated column outranks a `MAX`), replacing the per-`FindingKind` constant in `severity.py`. The fan-out absorbed by a grouping is a suppression (a guard collapsed the multiplicity), not a lower tier.

## Mapping a consumer

Classifying a consumer is two steps: find where the effect-carrying value is read, then run the effect's predicate on the reading operation.

- **Intra-model**: an AST walk from the effect's introduction (a join, an `UNNEST`) to the positions that read the affected columns in the same statement.
- **Cross-model**: the lineage graph, where the effect is a propagated property on a column and the consumer is a position in a downstream model that reads it. This is what `nullability` already does for the value effect.

For inflation the consumer set is every duplicate-sensitive read of any replicated-side column, found by joining the cardinality property (which side replicates) against `where_provenance` (which side a read traces to), not by inspecting the joined-in columns alone.

## Guards are one catalog

The two effect families share one `Guard` interface: a guard reports which effect it clears at a position, so a detector asks the catalog whether any guard clears its effect on the path. This replaces the per-detector logic (`_is_null_protected`, the COALESCE-fallback helper, the OR-sibling helper, the projection-merge check), which today each answer the same "can a padding NULL still be seen here" privately and do not compose.

The value-effect guards:

- `COALESCE(col, ...)` and `IS [NOT] NULL` (today's `_is_null_protected`).
- `COALESCE(nullable_col, non_nullable_fallback)`, where the fallback is a literal or an expression whose columns are all non-nullable.
- a top-level OR whose sibling disjunct keeps the unmatched rows alive.
- a downstream `WHERE col IS NOT NULL` filter, and an activated conditional `not_null`.

These compose: a `COALESCE` nested inside an OR is cleared by the union of the rules with no extra code. The cardinality-effect guards (collapse before a sensitive consumer, the outer/`LEFT` flatten form, `DISTINCT`) implement the same interface.

The projection-merge idiom in issue #139 is the same guard one clause over. `coalesce(a.k, b.k)` in the projection of a `FULL OUTER JOIN` recovers the merged key from whichever side matched, supplying a non-null value from the preserved side, exactly the COALESCE-fallback guard the `GROUP BY` case uses. So #139 and #169/#173 are one catalog entry.

## The cluster as one product

Every detector, issue, and open patch is a cell in (effect x consumer x guard).

| Detector / issue / PR | Effect | Sensitive consumer | Guard at stake |
| --- | --- | --- | --- |
| `null_group_after_outer_join` | value | GROUP BY key | COALESCE-to-nonnull, IS NOT NULL |
| PR #173 (closes #169) | value | GROUP BY key | COALESCE-with-nonnull-fallback |
| `where_on_outer_joined_nullable` | value | comparison in WHERE | COALESCE, IS NULL |
| PR #174 (closes #168) | value | comparison in WHERE | OR-sibling rescue |
| `coalesce_on_join_key` / #139 | value | ON-clause match vs projection merge | projection merge over FULL join is a guard; keep ON firing |
| `join_on_nullable_key` (inner/semi) | value | join-key match | not_null, filter |
| `not_in_nullable_subquery` | value | NOT IN (3VL) | NOT EXISTS, filter NULLs |
| `join_fanout` / #170 | cardinality (inflate) | duplicate-sensitive read of a replicated-side column | collapse before consumer (GROUP BY, qualify, pre-aggregate) |
| #63 (inner UNNEST/CROSS flatten) | cardinality (deflate) | the model's row-preservation contract | the LEFT/OUTER flatten form |

Issues #169 (the COALESCE-non-null-fallback rule) and #168 (the top-level-OR-sibling rule) landed as guard-catalog entries for the value effect, and #139 as the ON-clause scoping of `coalesce_on_join_key`. Issue #170 landed as the duplicate-sensitivity collapse guard on inflation, and #63 as the deflation cell. The in-flight PRs #173 and #174 are superseded by the catalog versions of their #169 and #168 rules.

## Relationship to the existing properties

A cardinality property is a composition over substrate that mostly exists.

- `nullability` is the value effect, propagated cross-model with outer-join taint and conditional clearing.
- `uniqueness` computes, for `join_fanout`, which side of a join lacks a covering key: the replicated side an inflation analysis needs.
- `functional_dependency` models grain, GROUP BY keys, candidate-key determinations, and inner-join `ON` equalities, and stays silent on outer-padded sides and on `UNNEST`. It is much of a cardinality property's grain reasoning.
- `where_provenance` records, per output column, the source columns it traces to: the fan-trap discriminator.
- `sql/aggregates.py` classifies aggregates on the magnitude-coherence axis; multiplicity-sensitivity folds into the same registry (see above).
- `_load_bearing_scopes` is a consumer-sensitivity classifier for the determinism effect.

The cardinality property is then an inflate/preserve/deflate value per column, grounded by join structure and array-flatten constructs, propagated with the firewall on its anchor, read by detectors that join it against `where_provenance` and the consumer predicate. Its anchor is a likelihood, so its findings carry lower severity than the proven-nullable ones.

## Refactor plan

Each step stands on its own. The first four are landed; the fifth is the follow-on.

1. **Extract the value-guard catalog.** *Landed* in `sql/guards.py` as a set of composable predicate functions (`is_coalesced`, `is_null_checked`, `supplies_present_value`, `rescued_by_or_sibling`) rather than a `Guard` class, since each detector composes the subset its consumer needs and the functions share one AST-walk primitive. `_is_null_protected` is folded in and deleted; the COALESCE-non-null-fallback rule (#169) and the top-level-OR-sibling rule (#168) are catalog entries. `null_group_after_outer_join` and `where_on_outer_joined_nullable` are retrofitted onto it and pinned with contract tests. The projection-merge case (#139) resolved one clause over: a COALESCE on a join key is a hazard only in the ON clause (the match condition), so `coalesce_on_join_key` is scoped there and the projection-list merge stays silent without a dedicated guard.
2. **Extract the value-effect consumer predicate.** *Folded, not separately extracted.* The set-level-consequence scope lives where each detector reads it (the `_NULL_INTOLERANT_COMPARISONS` set, the GROUP BY key walk, the join-key and NOT IN positions). Lifting these into one shared classifier is worthwhile alongside step 5, where the structural and property-grounded nullability detectors would both read it; on its own it did not earn the churn against the working cross-model detectors.
3. **Add the duplicate-sensitivity predicate.** *Landed* by extending `aggregates.py` into the single registry: `AggregateProfile` records a `duplicate_sensitive` boolean alongside the magnitude `behavior`, `strips_duplicates` reads the `DISTINCT` node, the adapter name-keyed extension is `AdapterProfile.duplicate_safe_aggregate_builtins`, and the firewall default makes an unclassified aggregate sensitive. The surface is one positive predicate, `duplicate_sensitive(agg)`, rather than a sensitive/insensitive pair. `join_fanout`'s local case (#170) suppresses when a GROUP BY collapses the join and every projection/HAVING consumer is duplicate-safe. This local rule reads the predicate off the registry directly; the `where_provenance` replicated-versus-joined discriminator is the refinement that arrives with the cross-model property in step 5.
4. **Land #63.** *Landed* as `detect_inner_flatten_row_drop`, a dialect-aware structural deflation detector reusing the row-preservation framing: the inner comma/`CROSS` form of an array flatten fires, the `LEFT`/`OUTER` form is silent.
5. **Lift cardinality to a propagated property** for the cross-model inflate and deflate cases, once the local detectors are proven. This makes the inherited fan-out and the cross-model row drop provable, mirroring the local-to-inherited path the nullability work took, and is where the shared consumer predicate (step 2) and the `where_provenance` fan-trap discriminator (step 3) pay for themselves.

## Soundness obligations

- The posture is split by gate: firewall on the effect anchor (fire only on a scarce positive fact), broad net on the consumer and guard (anchored and consumed, fire unless a proven-insensitive consumer or proven-clearing guard is recognised). Evidence tables allowlist only proven-safe operations and guards; an unrecognised operation keeps firing.
- We do not gate on the data realizing the harm; there is no runtime probe and the gate is not statically knowable, so a finding is a likely hazard, not a certain one.
- The value sensitivity predicate is grounded in 3VL and the inflation predicate in multiset algebra; each entry cites the semantics that put it on the insensitive side.
- The aggregate registry is one source of truth for both axes, type-keyed with a per-warehouse name extension; its multiplicity default is sensitive, so an unclassified aggregate never silently clears a fan-out.
- Inflation is cleared only when every duplicate-sensitive read of a replicated-side column is collapsed first, the replicated side read from the cardinality or uniqueness substrate and the trace from `where_provenance`.
- Guards compose: a finding past the union of applicable guards is a bug, and the union is the contract the catalog tests pin.
- The structural and property layers keep the non-local split `nullability-hazards.md` states, so the same construct is not flagged twice.

## To settle during implementation

- **Inflation reach across models.** Hypothesis: an un-collapsed fan-out degrades the producing model's output uniqueness, and `uniqueness` already propagates, so the cross-model additive-consumer check reduces to degraded uniqueness plus `where_provenance` with no new replicated-side value to carry. Settle with two or three cross-model fixtures (fan-out in a staging model exported without a collapse, `SUM`/`COUNT` of it in a mart), which double as step-5 acceptance tests. If an intermediate aggregate erases the trace, degrade to the introduction-site framing and document it.
- **Severity thresholds.** The inputs (anchor strength, consumer kind, effect) and the mapping skeleton are decided; the cut points are calibrated against the ~800-model corpus by labelling a finding sample and matching human triage.
- **Deflation filter guard.** A downstream filter that subsumes the dropped rows makes a deflation benign, but proving filter subsumption is expensive. Ship the outer-form-only detector, instrument it to count same-model filters on the dropped side, and build the guard only if that near-miss rate is material on the corpus.

## Prior art

The hazards are the documented surprises of SQL's three-valued logic and multiset semantics, charted in the relational-database literature and the SQL standard's treatment of unknown, with Date's critiques of nulls the classic reference for the value effect and the idempotent-versus-additive aggregate distinction the classic reference for the cardinality effect. The fan trap is long-named in dimensional-modelling practice. The firewall on the effect anchor is ordinary abstract interpretation, a sound monotone analysis falling back to top under uncertainty (Cousot and Cousot); the broad net on the clearing is the false-positive-tolerant linter posture the structural detectors already take, and the two compose into a flag for the likely problem. The cross-model reach rests on where-provenance (Cheney, Chiticariu, and Tan) and the propagation calculus (Green, Karvounarakis, and Tannen). The contribution is the observation that the cluster is one (effect x consumer x guard) product, that consumer sensitivity is derivable from each effect's algebra, and that the guards are a composing catalog rather than per-detector patches.
