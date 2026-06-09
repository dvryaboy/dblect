# Functional dependencies as a propagating property

Status: design notes. The algebra and the transfer rules are the worked part; the authoring spelling and the chase bound are the parts still open. This sketches how a functional dependency becomes a first-class fact the substrate carries, because it is the highest-leverage way to grow cross-model coverage: one declaration discharges a whole class of aggregation obligations many models away.

Audience: anyone building the property, or reasoning about why a `sum ... group by` that looked unsafe is actually fine. It assumes the substrate in [`lineage-facts.md`](lineage-facts.md), the uniqueness property it extends, the propagation calculus in [`propagation-soundness.md`](propagation-soundness.md), the summarizability treatment in [`domain-type-algebra.md`](domain-type-algebra.md), and the rigor model in [`cross-model-contracts.md`](cross-model-contracts.md).

## What a functional dependency is, and why it is already half-built

A functional dependency `X -> Y` says that within a relation, any two rows agreeing on the determinant columns `X` agree on the dependent columns `Y`. The case that motivates it is `country -> currency`: each country bills in one currency, so a money amount summed per country never mixes currencies even though the `currency` column was projected away before the rollup ([domain-type-algebra.md](domain-type-algebra.md) fixes the semantics, following Hurtado and Mendelzon on summarizability under dimension constraints).

The reason this is the natural next property is that the substrate already runs its special case. A candidate key `K` is the dependency `K -> *`, every column determined by `K`, and uniqueness already carries candidate keys on a lattice, flows them across relations through `relation_reduce`, and meets them at confluence. The nullability property already borrowed that same `CandidateKeySet` carrier for conditional facts. So a functional dependency is uniqueness generalized from "this set determines everything" to "this set determines that set," and the engineering is to lift the carrier from a key set to a dependency set rather than to build a new propagation from scratch.

## The algebra: a lattice of dependency sets

The value at a relation is a set of functional dependencies, read up to logical closure under Armstrong's axioms (reflexivity, augmentation, transitivity). Precision is implication: a dependency set `A` refines `B` when `A` implies `B`, so knowing more dependencies is more precise. The empty set is the top, "no dependency known," and the bound is uniqueness's lattice with the payload widened:

- **meet** (combine two sources on the same relation) is the closure of the union: every dependency either side proves holds.
- **join** (confluence, a `UNION ALL`) is the intersection of closures: only a dependency that holds in both arms survives, the same way a candidate key survives a union only when both arms carry it.
- **bottom** is the contradiction a structural property never reaches.

A genuine contradiction is not reachable from structure, so the bottom exists only to bound the lattice, exactly as in nullability and uniqueness.

## Transfer rules: how a dependency rides relational algebra

The rules are the textbook propagation of dependencies through the relational operators (Abiteboul, Hull, and Vianu), and they are what make the property cross-model. Each operator is sound by omission: where it cannot preserve a dependency it drops it rather than carrying a false one.

- **Selection (`WHERE`)** preserves every dependency, since a subset of rows still satisfies it, and it *introduces* dependencies: `WHERE c = <const>` grounds `{} -> c` (c is constant on the surviving rows), and that is one of the discharge paths for an aggregation.
- **Projection (`SELECT`)** keeps a dependency when both its determinant and dependent columns survive; dropping a determinant column loses the dependency unless it can be re-derived from others.
- **Join** carries each side's dependencies through, and mints new ones from the join structure: joining on `K` where `K` is a key of one side makes that side's columns functionally determined by `K` in the result. This is the structural inference behind the "let the join speak for itself" discharge, where `currency` arrives by a lookup keyed on `country` and the dependency `country -> currency` is read off the join with no declaration.
- **Group-by** makes the grouping key `G` a key of the output (`G -> *` over the result columns), since each output row is one group; dependencies among surviving columns carry if they range over `G` or its determined columns.
- **Union** keeps a dependency only when both arms prove it, the lattice join.
- **Opaque operators** (a window region, a recursive CTE, a UDF) are the type-erasing boundaries the substrate already marks. A dependency on the input does not automatically transfer to a derived output column, so the conservative rule drops it and asks for re-declaration at the boundary, the same treatment refinement types get there.

## Grounding: where dependencies enter

The discoverers mirror the ones the substrate already reads, so a project that has declared its schema in dbt gets dependencies for free:

- **A `unique` test or native key** on a column set grounds the candidate-key dependency `K -> *`. This is the uniqueness fact, read as a dependency.
- **A `relationships` test (a foreign key)** combined with a key on the referenced side grounds the join-derived dependency that the referenced attributes are determined by the key value. The same `relationships` test dblect already reads for foreign-key construction does double duty here.
- **A constant filter** grounds `{} -> c` at the scope where it holds, through the predicate engine that conditional-fact activation already uses.
- **A declared dependency** the author writes, `country -> currency`, grounds a vouched fact: checkable against data, but not proven from the SQL.

The first three are proven, so they propagate as structural facts with no runtime needed. The fourth is asserted, and that distinction is the whole of its verification story.

## Verification: a declared dependency is checkable, and its violation cascades

A declared `X -> Y` is exactly the kind of claim the rigor model in [`cross-model-contracts.md`](cross-model-contracts.md) calls a vouched fact, propagated and relied on but not proven from the SQL, so it earns a runtime oracle. The property-based testing loop gets one new intent:

- **FDViolation(X, Y).** Emit two rows agreeing on `X` and differing on `Y`. If the model under test admits them, the dependency is false on data, and the finding names the dependency *and the aggregations that relied on it*. A declared `country -> currency` that the generated data can violate is reported as its own finding, the cascade the algebra doc already promises: a country found billing in two currencies retracts the license it gave the per-country `sum`.

The structural soundness obligation is the same template the uniqueness and nullability soundness PBTs already discharge: generate random derivations over the operators above, propagate the dependency set, and assert against a warehouse oracle that every dependency the property claims actually holds on the data. The property may under-claim freely; it may never over-claim.

## The payoff: one mechanism for every aggregation discharge

The summarizability check for `sum(m) group by G`, where `m` carries a per-row tag `t` (a currency, a unit), is "is `t` constant within each group." Every discharge path in [domain-type-algebra.md](domain-type-algebra.md) collapses to one question once dependencies propagate: does the dependency set at this relation, together with the group key and the active filters, imply `G -> t` under Armstrong closure?

- `t` in the `GROUP BY` is `G -> t` by reflexivity.
- `t` pinned as a logical column is `{} -> t`.
- a declared or join-inferred `country -> currency` propagated here is `G -> t` directly.
- `WHERE t = <const>` is `{} -> t` at this scope.

So the aggregation check stops being four special cases and becomes one closure query against the propagated dependency set, with the predicate-implication engine supplying the filter half. That is the leverage: the same fact that discharges the rollup also powers join-key reasoning, redundant-group-by elimination, and the currency-coherence check, and it travels the DAG on the engine that already moves keys.

## What the DSL needs

The authoring surface is small, and most of it is settled by reusing the contract-method shape.

- **Declaration** is a contract method returning a symbolic claim over column proxies: `self.country.determines(self.currency)`, with a multi-column determinant written `self.columns(a, b).determines(self.c)`. The operator spelling (a `determines(...)` call versus a `>>` sugar) is open, and it is the only genuinely unsettled part.
- **Placement** follows the rigor model's rule that a fact lives where it holds: the dependency is declared on the `ModelContract` for the relation that grounds it, and cross-model use is automatic through propagation. A dependency needed on a derived relation that does not itself ground it is either re-declared there or must be provable by the transfer rules; authoring convenience does not move it to a model where it cannot be checked.
- **The finding** when a dependency is violated names the dependency and the discharges it retracts, so the cascade is legible rather than a distant `sum` lighting up for no visible reason.

## Open questions

- **The `determines` spelling.** A method call versus a `>>` operator sugar, and whether a determinant of several columns reads best as `self.columns(...).determines(...)` or a tuple. Settle against a real multi-currency project.
- **How far to chase.** General dependency implication is expensive in the worst case, though the practical fragment (a single dependent, small determinants) is cheap. Bound the closure and degrade to "not proven" gracefully rather than chasing without limit, the same conservative posture the rest of the framework keeps.
- **Re-declaration across opaque boundaries.** A dependency dropped at a window region or UDF must be re-declared at the boundary to survive. How much of that re-declaration can be inferred, and how much the author must restate, wants a real model with a window-shaped rollup to decide.
- **Inferred versus declared trust.** A join-inferred dependency is proven; a declared one is vouched and carries a runtime oracle. Whether the report distinguishes them visibly, or treats them uniformly until one is violated, is the same trust-visibility question the rest of the surface raises.

## Prior art

Functional dependencies and their inference are the foundation of relational normalization, with Armstrong's axioms the complete proof system for implication and the chase the standard procedure for reasoning about them (Abiteboul, Hull, and Vianu collect both). Query optimizers have long propagated dependencies through plans to remove redundant sorts and groupings, which is the same transfer this property runs, turned toward meaning rather than performance. The application to aggregation correctness is summarizability under dimension dependencies (Hurtado and Mendelzon; Lenz and Shoshani for the measure-and-category compatibility it specializes). dblect's contribution is to carry these dependencies across dbt model boundaries on the substrate that already moves keys, to ground them from the `unique` and `relationships` tests a project has already written, and to verify a declared one with generated counterexamples rather than trust alone.
