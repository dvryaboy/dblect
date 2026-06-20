# Nullability hazards: catching the goofs without the confetti

Status: design notes. The firewall principle and the introducer catalog are worked through; the detector roster and the local-versus-inherited gate are the parts still open.

Audience: anyone building the detectors that consume the nullability property, or reasoning about why those detectors stay quiet on healthy models. It assumes the propagation calculus in [`propagation-soundness.md`](./propagation-soundness.md), the substrate in [`lineage-facts.md`](./lineage-facts.md), and the column engine in [`column-level-lineage.md`](./column-level-lineage.md).

## The promise and the trap

A nullable column changes the meaning of the ordinary operations analysts reach for every day, and it does so silently. A `JOIN` on a nullable key drops the rows whose key is null, because null never equals null. A `GROUP BY` on a nullable column gathers every null row into one phantom group that downstream code rarely expects. `x NOT IN (subquery)` collapses to an empty result the moment the subquery yields a single null. None of these raise an error; the build stays green and the numbers quietly shift. This is the meaning-level class dblect exists to catch, and nullability is the property that sees it.

The trap is that nullable is the *default*. In SQL a column is nullable unless something constrains it, so a detector that fires whenever a column "might be null" fires on almost every column in the project. That is a confetti cannon: thousands of findings, no signal, uninstalled by lunch. The whole design problem is to fire on the goofs and stay silent everywhere else.

## The firewall: fire on proof, never on default

The substrate already states the principle that makes this tractable. From [`propagation-soundness.md`](./propagation-soundness.md): top is the only value a rule may emit without proof, and every value below top is a positive claim. `NULLABLE` means "a null was proven to reach here," and `UNKNOWN` (the top) means "no information." A rule may emit `NULLABLE` only where the SQL establishes it, never as a fallback on uncertainty.

That gives us the firewall in one sentence: **a nullability hazard detector consumes only proven `NULLABLE`, never `UNKNOWN`.** A column the property left at `UNKNOWN` produces no finding, because the framework never proved a null can occur there. The default state of the world is silence, and a finding is a positive claim that this column carries nulls into a position that mishandles them.

This is the same opportunistic posture the uniqueness detectors already take, with the polarity flipped. Uniqueness fires on the *presence* of a scarce fact (a known key that fails to cover an operation); the valuable, hard-won fact there is "there is a key." Nullability fires on the presence of the other scarce fact (a proven null reaching a null-sensitive position); the valuable fact here is "this is non-null" or "this is provably nullable," and mere absence of a `NOT NULL` declaration is worth nothing. Both lead with proof, so neither sprays the project.

## Where `NULLABLE` is born

The firewall has a consequence that shapes the whole effort. Today the nullability property only ever *grounds* `NON_NULL`, from `not_null` generic tests and native `NOT NULL` constraints; every undeclared column rests at `UNKNOWN`. There is no source of positive `NULLABLE` evidence in the property at all. So under the firewall the detectors would fire on nothing. The enabling work is not the detectors; it is giving the property places to *prove* a null.

These are the sound introducers of `NULLABLE`, in rough order of how much of the value they carry:

- **The optional side of an outer join.** A `LEFT JOIN` makes every column drawn from its right side nullable in the result, even a column that is `NON_NULL` at its own source, because unmatched left rows pad those columns with nulls. `RIGHT JOIN` mints on the left side, `FULL JOIN` on both, `INNER` and `CROSS` mint nothing. This is the headline source, and it is the one that travels across models: the optional-side column flows downstream still nullable, while the SQL that consumes it three models later shows a plain column name with no local cue that it was ever optional. This introducer is relation-aware (it reads the join structure, not a single column expression), so it lives alongside the relation walk that uniqueness already runs, not in the per-column operator catalog.

- **Null-introducing scalars.** `NULLIF(a, b)` is null whenever `a = b`; a `CASE` with no `ELSE` is null on every unmatched row; a bare `NULL` literal and an outer-applied subquery that can return no row are null by construction. These are ordinary per-column operator transfers, the same shape as the existing `COALESCE` and `IS NOT NULL` rules, and they mint `NULLABLE` locally and cheaply.

- **Confluence of a nullable arm.** A `UNION ALL` whose one arm is proven nullable yields a nullable column. The property's existential semiring already does this correctly the moment an arm carries a proven `NULLABLE`; no new rule is needed, only an upstream introducer to give the arm its value.

We never mint `NULLABLE` from observed data. A column is proven nullable by the *shape* of the SQL, not by a null someone happened to see in a sample. Static stays static, and the runtime layer is where observed nulls would earn their own, separately-justified finding.

The dual direction matters as much: every introducer has guards that clear the taint back to `NON_NULL`, and those guards are how a healthy model stays silent. `COALESCE(optional_col, 0)` is non-null again (the existing operator rule already handles this). A downstream `WHERE optional_col IS NOT NULL` removes the null rows. An activated conditional `not_null` fact (the `where`-filtered tests the substrate already carries) re-establishes `NON_NULL` at the scope where its predicate holds. A detector that fires past any of these guards is a false positive, so the guards are part of the contract, not an afterthought.

## Surprise is the signal, which is why this is cross-model

The static analyzer already ships structural detectors for the locally-visible cases: `null_group_after_outer_join`, `where_on_outer_joined_nullable`, and `coalesce_on_join_key` each read a single model's AST and flag a hazard whose cause sits right there in the same `SELECT`. They are a deliberately broad net. As their module says, the static layer has no lineage, so it prefers false positives to false negatives and lets a typed contract or an ignore comment quiet the noise.

The nullability property earns its keep on the case those detectors structurally cannot see: nullability introduced *upstream* and inherited. The optional-side column from a `LEFT JOIN` in `stg_orders` arrives in `customers` as a plain `customer_id`, and the analyst grouping by it has no local signal that it can be null. The AST of `customers` shows an innocent column. Only a walk back through the model graph proves the null is there. That inherited, non-local nullability is the goof a human reviewer misses precisely because the cause and the symptom live in different files.

So **surprise is the ranking signal**, and surprise is what the property measures that a local detector cannot. Two design consequences follow. First, the property-based detectors should concentrate on the non-local case, both to avoid double-flagging the structural detectors and to spend the user's attention where no other tool looks. Second, the finding should carry the provenance that makes the surprise legible: not just "`customer_id` is nullable" but "`customer_id` is nullable because `stg_orders` left-joins it onto `orders`." The where-provenance property already records the source columns a value traces to; the introduction site is the extra witness worth carrying so the explanation, not just the flag, lands in the report.

## The detector family

Each detector sits on a null-sensitive position, consumes a column the property proved `NULLABLE`, and stays silent on `UNKNOWN`, on `NON_NULL`, and behind any clearing guard.

- **Nullable join key (silent row loss).** A join equates a column the property proved nullable. The null-keyed rows drop, so the join is quietly an inner join on those rows even when written as an outer one. Highest value, and almost always cross-model, since the nullability usually rode in from an upstream optional side.

- **Nullable group-by key (phantom group).** A `GROUP BY` references a proven-nullable column, gathering every null row into one group the consumer rarely models. This generalizes the existing structural null-group detector across model boundaries: the structural one needs the outer join in the same `SELECT`, while this one fires when the nullability was inherited. It escalates when the phantom group is then dropped downstream (the jaffle `customers.sql` shape, where the null group's total silently vanishes through a later join), which is the difference between a curiosity and lost money.

- **`NOT IN (subquery)` with a nullable projected column.** The canonical three-valued-logic footgun: one null in the subquery makes the whole predicate never-true, so the result is silently empty. Narrow as a construct and high-severity when present; it falls out of the same machinery for free and is a clean first consumer to prove the property end to end.

- **Comparison predicate on a proven-nullable column (candidate).** A `WHERE nullable_col = x` (or `<`, `>`, `IN`, `BETWEEN`, `LIKE`) drops the null rows. This generalizes `where_on_outer_joined_nullable` beyond the locally-visible outer join to any proven nullability. It overlaps the structural detector enough that merging the two, or gating this one to the inherited case, is an open question below.

## Relationship to the structural detectors

The two layers are complementary, and the split is clean.

| | Structural (AST) | Property (lineage) |
| --- | --- | --- |
| Sees | one model's SQL | the whole upstream graph |
| Posture | broad net, false-positive-tolerant | precise, silent unless proven |
| Catches | locally-visible cause | inherited, non-local cause |
| Quiets with | ignore comment / contract | a clearing guard or a `NON_NULL` proof |

The de-duplication rule keeps them from talking over each other: a property-based detector fires only when the nullability is introduced non-locally, leaving the same-`SELECT` cases to the structural detector that already owns them. Over time some structural detectors may retire into their property-based generalization, but the broad net stays valuable on projects that declare little, where the property has little to prove from.

## Adjacent hazards this property does not own

Flattening an array with an inner lateral (`FROM t, UNNEST(t.arr)`, `CROSS JOIN UNNEST(...)`, `CROSS JOIN LATERAL ...`) silently drops the parent row whenever the array is empty or null. That looks like a nullability bug and is worth catching, but it is a row-preservation (cardinality) hazard, the deflation twin of join fanout: fanout multiplies rows, this annihilates them. The nullability property can prove only the null-array half of it and structurally misses the more common empty-array half (`[]` is a non-null, zero-length value), so it stays out of scope here and lives as a structural detector instead. A future collection-emptiness property could gate it precisely, reusing this engine's existential shape, once the static introducers for "can be empty" are rich enough to clear the firewall. Tracked separately as a `sql/patterns.py` detector.

## Confirming a hazard at runtime

A static finding here states that a hazard exists; it cannot show how many rows the hazard moves today. That blast-radius question is what separates an active defect from a dormant one, and it is easy to misjudge by reading the SQL alone. The runtime layer answers it. Each of these findings doubles as a generation seed: it has already localized the join, the columns, and (for the WHERE case) a suggested guard, which is enough to pick an intent and pin its parameters. Running the model as written against the same model with the guard applied yields a row-set delta that is both the proof and the triage signal, a non-empty delta marking an active defect with a witness row and an empty delta marking the finding dormant. See "Static findings as intent seeds" in [contract-directed-generation.md](contract-directed-generation.md) for how this rides the same intent catalog and example memory as the contract-directed path.

## Implementation sketch

Grounded in the current code, the work is three slices that can land in order, each independently useful.

1. **Mint local `NULLABLE`.** Add `NULLIF`, `CASE`-without-`ELSE`, and the `NULL` literal to `NULLABILITY_OPERATORS` in `properties/nullability.py`. This is the smallest change that gives the property a real tri-state and lets the `NOT IN` detector fire on the local case.
2. **Mint outer-join `NULLABLE`.** Teach the relation walk to mark columns drawn from an outer join's optional side as `NULLABLE`, mirroring how `relation_reduce` already reads join structure for uniqueness. This is what makes the property cross-model and is where the headline value lives.
3. **The detectors.** A new module parallel to `uniqueness/detector.py`: propagate the nullability property once over the relation graph, curry the detectors against the resulting per-column annotations and a per-tree scope index, and join them into `make_fact_grounded_detectors` so they run in the existing single pass. New `FindingKind` members for the join-key, group-key, and `NOT IN` hazards; the `where_on_outer_joined_nullable` kind is reused or generalized depending on the open question below.

The conditional `not_null` activation already built (the `where`-filtered tests flowed across relations) plugs in as a clearing guard: a scope where an activated `NON_NULL` holds suppresses the hazard there. That gives the activation work its first real consumer.

## Soundness obligations

- Only proven `NULLABLE` produces a finding. `UNKNOWN` never fires. This is the firewall and the rest depends on it.
- The outer-join introducer marks only the genuinely optional side: nothing on `INNER` or `CROSS`, the right side on `LEFT`, the left on `RIGHT`, both on `FULL`. Over-minting here is the most likely source of false positives, so it is the first thing property-based tests should pin.
- Every clearing guard is honored before a finding is emitted: `COALESCE` and `IS NOT NULL` and an `IS NOT NULL` filter and an activated conditional `not_null`. A finding past a guard is a bug.
- The non-local gate is respected, so the property layer and the structural layer do not both flag the same construct.

## What it catches in the demo

The install-day jaffle finding (the `customers.sql` null group whose total is silently dropped) is exactly this property's home case, lifted from the structural approximation to a proven, explained, cross-model finding. The structural detector flags the shape; the property proves the `customer_id` feeding the `GROUP BY` is nullable because of the upstream `LEFT JOIN payments to orders`, names the source, and shows the consequence chain to the dropped group. Same headline catch, with the provenance that turns "look at this" into "here is the goof and here is where it came from."

## Open questions

- **Group-by on nullable: fire alone, or only when downstream-dropped?** The bare phantom group is sometimes intended; the dropped group rarely is. Gating on the downstream drop cuts noise at the cost of missing the cases where the drop is further away than the walk looks.
- **How to carry the "why nullable" witness.** Options: extend the nullability value with the introduction site, or keep the value a plain tri-state and join against where-provenance at finding-construction time. The second keeps the property minimal; the first keeps the explanation local to the proof.
- **Local versus inherited threshold.** Is "introduced non-locally" the right gate, or should the property-based detectors rank every hazard by surprise and show all of them, letting the user set a cut? The gate is simpler; the ranking is more honest about a continuum.
- **The comparison-predicate detector: merge or keep separate?** It overlaps `where_on_outer_joined_nullable`. Merging gives one detector with a richer nullability source; keeping them separate preserves the broad-net structural version for low-declaration projects.
- **Interaction with the conditional `not_null` activation.** Confirm an activated `NON_NULL` at a scope cleanly clears the hazard there and does not leave a stale `NULLABLE` from an upstream introducer.

## Prior art

The hazards themselves are the well-documented surprises of SQL's three-valued logic, charted thoroughly in the relational-database literature and the SQL standard's treatment of unknown; Date's critiques of nulls are the classic reference for why these operations behave as they do. The firewall is ordinary abstract interpretation: a sound, monotone analysis that falls back to top under uncertainty (Cousot and Cousot), which is what lets us promise silence rather than noise. The cross-model reach rests on the provenance line the substrate already draws on, where-provenance from Cheney, Chiticariu, and Tan and the propagation calculus from Green, Karvounarakis, and Tannen. Linters and type systems have long tracked nullability statically, from nullable-type checkers in general-purpose languages to schema-aware SQL analyzers; dblect's contribution is to carry that proof across dbt model boundaries and attach it to the specific analytic operations where a null changes a result, with the provenance that explains the surprise.
