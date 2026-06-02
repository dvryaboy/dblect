# Propagation soundness: the obligations a property meets

Status: reference. Maintained as the propagation calculus grows, independently of any single design doc.
Audience: anyone adding or reviewing a `Property`, built-in or compiled from a user declaration, or reasoning about why a propagated annotation can be trusted. It assumes SQL; it defines the lattice and semiring vocabulary it leans on as it goes, and draws on abstract interpretation, provenance semirings, and functional-dependency propagation, collected in the references.

This doc states what every property must satisfy for the single-pass propagator to produce annotations the audit can trust, and why those are the right obligations. The substrate that grounds leaf values from declarations is [`lineage-facts.md`](./lineage-facts.md); the engine that walks the graph is [`column-level-lineage.md`](./column-level-lineage.md). This is the calculus underneath both.

## A property is an abstract domain

A **property** is a value type `K`, a lattice over `K`, and rules for moving a `K` through SQL. `K` is ordered by *precision*: a more precise value commits to more about the data. Nullability ranges over `{NON_NULL, NULLABLE, UNKNOWN}`; uniqueness over candidate-key sets, where knowing more keys is more precise; a user-domain axis over whatever bounded lattice it needs (a two-value `contains_tax`, an enum currency, an interval range).

A **lattice** equips `K` with four operations, and the rest of this doc leans on them by name. The **top** is "no information," what a rule emits when the SQL tells it nothing (`UNKNOWN` for nullability). The **meet** of two values is the most precise value consistent with both, used to fold several declarations about one node together (a `not_null` test and a `nullable: true` flag meet to the stronger, non-null guarantee). The **join** of two values is the least precise value that still covers both (`NON_NULL` joined with `NULLABLE` is `NULLABLE`, because a result drawn from both branches can contain a null). The **bottom** is "contradiction," a value no data satisfies, reached only when two declarations genuinely disagree (a column declared both `contains_tax = TRUE` and `contains_tax = FALSE`).

The engine gives every node a value by combining its inputs with one rule per SQL operator, the property's *transfer rules*. Two requirements keep this honest:

- **Sound.** A rule never reports a value more precise than the SQL guarantees. When in doubt it falls back to the lattice top.
- **Monotone.** Feeding a rule a more precise input never yields a less precise output.

These are the abstract-interpretation obligations (Cousot and Cousot). Together they make the whole walk safe to trust.

One asymmetry is worth stating plainly, because it is a common implementation footgun. **Top is the only value a rule may emit without proof.** Every value strictly below top is a positive claim. `NULLABLE` means "I proved a null can occur here," not "I have not proved non-null." A rule may emit it only where the SQL establishes it, never as a fallback on uncertainty; emitting an intermediate value by default would let an unproven `NULLABLE` false-conflict a declared `NON_NULL`.

## Two structures: abstraction domain and operator algebra

Two structures do separate jobs, and naming them apart keeps the engine honest.

The **abstraction domain** is the precision lattice just described. Its work is to compare values for precision: resolution folds several declarations about one node with the meet, and the consistency check asks whether an inferred value refines a declared one.

The **operator algebra** is how values combine at the two SQL operators that merge data from more than one source. A **confluence** stacks several branches into one output, as `UNION ALL` does; the **confluence combine** is the property's rule for merging the branch values there. A **cross** is a `JOIN`, where two relations combine into one; the **cross combine** is the property's rule there.

For many properties the confluence combine turns out to be exactly the lattice join, and then the two structures coincide. This holds for any **idempotent** property, one whose combine of a value with itself returns that value. Nullability is idempotent: a `UNION ALL` of two nullable branches is nullable, and the join of `NULLABLE` with itself is `NULLABLE`. Such a property needs no operator algebra beyond its lattice.

A counting property breaks the coincidence, and seeing why is the clearest way to understand the split. Take a property tracking how many rows each output stands for. At a `UNION ALL` of a two-row branch and a three-row branch the output has five rows, so the confluence combine is addition. Addition is not the join of any lattice on counts, because a lattice join is idempotent (`x` joined with `x` is `x`) while addition is not (`2 + 2` is `4`, not `2`). The combine therefore lives outside the precision lattice and has to be supplied separately.

That separate structure is a **semiring**: a `plus` for confluences (the `UNION ALL` combine, addition for counting) and a `times` for crosses (the `JOIN` combine, multiplication for counting, since joining an m-row relation with an n-row relation yields up to m times n rows), each with an identity element. A `Property` carries an optional `semiring` slot, filled for the counting and accumulating properties whose combines are not lattice operations, and left empty for the idempotent and **value-domain** properties (the value-domain ones track a developer-declared meaning on a column's values, such as currency or tax inclusion). For those, the operator algebra is just the lattice join.

## Transfer rules by operator

A property's behaviour is indexed by relational operator, and most of it is forced by the lattice rather than chosen.

- **Filter / selection.** Preserve. Forced.
- **Confluence (`UNION ALL`).** The property's confluence combine. For an idempotent property this is the lattice join (nullability: nullable if either branch is; uniqueness: a key survives only if both branches carry it), forced by the lattice. A counting property supplies the semiring `+` instead. `UNION DISTINCT` is the same confluence with one operator-specific addition for uniqueness: set semantics removes duplicate rows, so the whole projected row is a *superkey* (a column set that is unique, though perhaps not minimal). The rule is therefore keyed on the specific operator.
- **Cross (`JOIN`).** For a column-scoped property there is no cross-column combine at the join: each output column traces to exactly one input column, so the `JOIN` is projection, and a column property's only real combines are confluence and multi-input scalars. The genuine cross combine appears for relation-scoped uniqueness, which combines keys across sides subject to join-condition coverage. For a counting property the cross is the semiring `×`.
- **Scalar / projection.** Preserve, transform, or clear. A genuine choice. An identity (`Alias`, a bare `Column`) preserves and is where tightening happens; a declared map (a currency conversion, a discount or tax annotation) transforms; an opaque scalar or bare literal clears to top. A binary combine (`a + b`) preserves when operands agree, raises a finding when two committed operands are incompatible, and clears to top when a committed operand meets an unrefined one.
- **Aggregation.** The aggregate rule, whose behaviour is the measure's *combinability* (whether its meaning survives a `GROUP BY`). A genuine choice, treated below.

So a property chooses behaviour only at scalar transforms and at aggregation; the rest follows from the lattice.

## Two families of properties

Properties split into two families by where their transfer rules come from, and that difference is the whole of what separates a structural property from a user-domain one. The engine treats both the same way; only the source of the rules differs.

- **Structural properties** run on **framework transfers**, theorems about SQL semantics that hold in every project: a `JOIN` multiplies cardinality, `DISTINCT` makes its projection a key, `COALESCE(x, 0)` is never null. Nullability, uniqueness, cardinality, grain (what one row stands for), and ordering are this proven core, verified once.
- **User-domain properties** run on **user transfers**, which rest on a declared *signature*, the author's statement of what an operation does to a meaning. Whether `revenue * 0.9` preserves tax inclusion is what the author meant by that line, which the framework cannot derive. Currency, tax inclusion, gross/net, and the other declared axes build from these, in an open catalog users extend.

A finding carries the assumptions in its derivation. One built only from framework transfers is unconditional; one that passes through a user signature holds given that signature. What must be proved differs while the machinery does not: the framework proves its own rules once, and a user rule is correct as long as the author's signature behaves as declared. The author *vouches* for that signature, a claim the framework trusts rather than proves, and the runtime layer catches an inaccurate vouch by testing it against generated data.

## Plan-shape independence

**Plan-shape independence** means two SQL expressions that compute the same relation get the same annotation, even when an optimiser rewrote one into the other: joining `A` then `B` annotates the same as joining `B` then `A`, and pushing a filter below a join changes nothing. Without it an annotation would track incidental query shape rather than meaning. The guarantee has three sources, one per family of property.

- **Semiring distributivity.** For a counting or accumulating property carrying a `semiring`, the law that `times` distributes over `plus` (`a × (b + c)` equals `(a × b) + (a × c)`) is what lets a join distribute over a union whichever way the query was written, so equivalent expressions agree (Green, Karvounarakis, Tannen). The semiring laws are the obligation; when they hold, plan-independence is free.
- **Confluence only, no join combine.** A value-domain or idempotent column property has no cross combine to define at a join, because each output column is a copy of exactly one input column (a join projects columns through; it does not blend two columns' annotations into one). With no cross combine, a join has nothing to distribute over, and plan-independence reduces to the confluence combine being associative and commutative, which it is, since it is the lattice join.
- **Key and functional-dependency propagation.** Uniqueness propagates candidate keys rather than counting, following classical relational theory (Abiteboul, Hull, Vianu), which is why it carries no `semiring`. Its `JOIN` combine reads the join predicate, so it is not a plain binary operation on `K`. For an **equijoin** (a join whose condition equates columns, like `orders.id = items.order_id`) the technique is to treat the equated columns as one (a column-equivalence, or quotient) and combine the resulting key sets; the plan-independence theorem is the classical result that propagated keys are invariant under join reordering across the select-project-join core of relational algebra. The detailed rules and proof live with the engine in [`column-level-lineage.md`](./column-level-lineage.md).

## Aggregation

The aggregate rule asks whether a measure's meaning survives a `GROUP BY`, and under what precondition (a *measure* is a numeric column an aggregate folds, like an amount that gets summed). Three outcomes cover it: preserved (the meaning carries straight through), preserved under coherence (it carries through only where each group is constant on some named columns), or cleared (no aggregate preserves it, as for a ratio). The rule is two pieces so its soundness obligation stays checkable.

- A pure **`core`** (`AggFunc × Annotation -> Annotation`, with no dependency read). The core is what must **commute with confluence and cross**, so the single-pass walk gives the same annotation whether an aggregate sits above or below a `UNION ALL`. Because it is pure, it is discharged in isolation: property-tested directly for a value-domain property, and for a counting or accumulating property it is the semimodule homomorphism law (the aggregate commutes with the semiring's `plus` and `times`; Amsterdamer, Deutch, Tannen), inherited once the property supplies its semimodule, which is the aggregate's algebra over `K`.
- An optional **coherence guard** that reads a functional dependency and clears to top where it fails (the mixed-currency `SUM`). Clearing to top commutes with confluence trivially, since top absorbs (combining anything with top yields top), which is exactly why factoring coherence out of the core lets the commutation law be stated over a pure function. The guard's plan-stability rests on the FD property it reads being itself plan-independent.

Aggregation is the one place the bare lattice does not force the rule, which is why it gets its own slot.

## Cross-property dependencies

A few properties read another's annotations to compute their own transfers: cardinality reads uniqueness to tell a fan-out join (one that multiplies rows, because the join key is not unique on the other side) from a key-preserving one; a user-defined money type reads a functional dependency to decide whether a grouped sum keeps its currency. A property names the properties its transfers read in `depends_on`, the propagator evaluates those first, and a transfer reaches a dependency only through a read-only, typed channel.

Two obligations keep this sound.

- **Acyclic.** The `depends_on` graph is acyclic, so the properties evaluate one after another in a fixed order and no pair has to be solved together by iterating to a mutual fixpoint, which is what keeps the walk single-pass.
- **Monotone in the dependency.** A transfer that reads a dependency is monotone in the dependency's value as well as its own input, so a dependency degrading toward top can only make the transfer more conservative, never let it claim more. A silent dependency reads as top, the most conservative input, so the absent case holds by construction.

## Determinism of the walk

The propagator carries an `Annotation`, a value plus two diagnostic bits (an `opacity` tag and an error-recovery `provisional` taint). Soundness and monotonicity are claims about the `value` only. The two bits are diagnostic metadata, outside the precision order, and they are deterministic functions of the walk: the lineage graph is a DAG (a directed acyclic graph) visited in dependency-then-topological order, so each node's full annotation is fixed by its inputs. `provisional` is non-monotone along dataflow on purpose (it clears on a fresh consistent anchor, which is error recovery) and is read only to downgrade a finding's severity, never to license a more precise value or suppress a sound finding. So the value's soundness is independent of the taint.

## Leaf facts are conditional bets

Framework transfers are theorems; the leaf values that seed them are not. A candidate key, foreign key, or `not_null` claim is an assertion the framework cannot verify, because it never reads source data. So even a core conclusion is conditional: given the declared leaf facts, the propagated values are theorems. "Proven" means proven from the declared inputs, not verified against data.

A constraint the warehouse declares but does not enforce is a leaf-fact risk, not a transfer-rule risk: it can make a propagated annotation wrong about the data while the rules that produced it stay sound. Many warehouses treat `PRIMARY KEY`, `UNIQUE`, and `FOREIGN KEY` as informational; some support a `RELY` form the optimiser trusts without validating, the same conditional bet this calculus makes. The question that matters collapses to one: is the claim checked against data by something that runs? A dbt `unique` test is, because the runtime layer runs it; an advisory `PRIMARY KEY` is not. The runtime layer is the backstop that turns a silent assumption into a checked one, and where a load-bearing annotation rests on an unenforced constraint that nothing tests, that gap is worth a finding. The finding mechanics live in [`lineage-facts.md`](./lineage-facts.md).

## The obligations as a checklist

Each is a property-test target.

- **Sound and monotone transfers**, over the annotation's `value`. Top is the only value emittable without proof.
- **Lattice laws** (associativity, commutativity, idempotence of meet and join, absorption, the top/bottom identities) and the derived consistency check.
- **Semiring laws** when a `semiring` is present (associativity, commutativity, distributivity, the identity and annihilation roles, and `plus == lattice.join` for an idempotent semiring). This is what buys plan-independence for the counting and accumulating properties.
- **Aggregate commutation** of each rule's `core` with confluence and cross, checked without a dependency context; the coherence guard tested separately for clear-on-failure.
- **Dependency monotonicity** for any transfer that reads `depends_on`.
- **Walk determinism**: the same graph and the same leaf values yield the same full annotation.

## References

Abstract interpretation (Cousot and Cousot) is the framework this engine is an instance of, and the source of the monotone-transfer and sound-over-approximation obligations. Provenance semirings (Green, Karvounarakis, Tannen 2007) and functional-dependency propagation (Abiteboul, Hull, Vianu) supply the algebra's shape, the first for the counting and accumulating properties and the second for uniqueness. Aggregate provenance (Amsterdamer, Deutch, Tannen 2011) is why aggregation gets its own rule with a commutation obligation rather than riding the bare lattice. The why-provenance and hypothetical-query line (Karvounarakis and collaborators) is the model for evaluating one derivation under different world assignments. The type-qualifier tradition (CQual, FlowCaml) is the closest analogue for the user-domain lattice, and the gradual-typing tradition (Siek and Taha; Wadler and Findler on blame) for the typed/untyped seam. SQL formal semantics (HoTTSQL, Cosette) underpins the operator rules.
