# Propagation soundness: the obligations a property meets

Status: reference. Maintained as the propagation calculus grows, independently of any single design doc.
Audience: anyone adding or reviewing a `Property`, built-in or compiled from a user declaration, or reasoning about why a propagated annotation can be trusted. It assumes SQL and a passing familiarity with lattices and semirings, and draws on abstract interpretation, provenance semirings, and functional-dependency propagation, collected in the references.

This doc states what every property must satisfy for the single-pass propagator to produce annotations the audit can trust, and why those are the right obligations. The substrate that grounds leaf values from declarations is [`lineage-facts.md`](./lineage-facts.md); the engine that walks the graph is [`column-level-lineage.md`](./column-level-lineage.md). This is the calculus underneath both.

## A property is an abstract domain

A **property** is a value type `K`, a lattice over `K`, and rules for moving a `K` through SQL. `K` is ordered by *precision*: a more precise value commits to more about the data. Nullability ranges over `{NON_NULL, NULLABLE, UNKNOWN}`; uniqueness over candidate-key sets, where knowing more keys is more precise; a user-domain axis over whatever bounded lattice it needs (a two-value `contains_tax`, an enum currency, an interval range).

The engine gives every node a value by combining its inputs with one rule per SQL operator, the property's *transfer rules*. Two requirements keep this honest:

- **Sound.** A rule never reports a value more precise than the SQL guarantees. When in doubt it falls back to the lattice top.
- **Monotone.** Feeding a rule a more precise input never yields a less precise output.

These are the abstract-interpretation obligations (Cousot and Cousot). Together they make the whole walk safe to trust.

One asymmetry is worth stating plainly, because it is a common implementation footgun. **Top is the only value a rule may emit without proof.** Every value strictly below top is a positive claim. `NULLABLE` means "I proved a null can occur here," not "I have not proved non-null." A rule may emit it only where the SQL establishes it, never as a fallback on uncertainty; emitting an intermediate value by default would let an unproven `NULLABLE` false-conflict a declared `NON_NULL`.

## Two structures: abstraction domain and operator algebra

Two structures do separate jobs, and keeping them apart is what keeps the engine honest.

- The **abstraction domain** is the precision lattice. It powers resolution (the meet of several declared values) and the consistency check (does an inferred value refine a declared one).
- The **operator algebra** is how values combine at SQL operators: the confluence combine at a `UNION ALL`, the cross combine at a `JOIN`.

For an idempotent property the confluence combine is exactly the domain join, which is why the two look like one thing. They are not one thing in general. A counting property's confluence is addition, which is the join of no lattice on counts. A `Property` carries an optional `semiring` slot for the counting and accumulating properties whose confluence is a non-idempotent `+`; it is left unset for the idempotent and value-domain properties, whose operator algebra needs no structure beyond the lattice.

## Transfer rules by operator

A property's behaviour is indexed by relational operator, and most of it is forced by the lattice rather than chosen.

- **Filter / selection.** Preserve. Forced.
- **Confluence (`UNION ALL`).** The property's confluence combine. For an idempotent property this is the domain join (nullability: nullable if either branch is; uniqueness: a key survives only if both branches carry it), forced by the lattice. A counting property supplies the semiring `+` instead. `UNION DISTINCT` is the same confluence with one operator-specific addition for uniqueness: set semantics dedups, so the whole projected row is a superkey. The rule is therefore keyed on the specific operator.
- **Cross (`JOIN`).** For a column-scoped property there is no cross-column combine at the join: each output column traces to exactly one input column, so the `JOIN` is projection, and a column property's only real combines are confluence and multi-input scalars. The genuine cross combine appears for relation-scoped uniqueness, which combines keys across sides subject to join-condition coverage. For a counting property the cross is the semiring `×`.
- **Scalar / projection.** Preserve, transform, or clear. A genuine choice. An identity (`Alias`, a bare `Column`) preserves and is where tightening happens; a declared map (a currency conversion, a discount or tax annotation) transforms; an opaque scalar or bare literal clears to top. A binary combine (`a + b`) preserves when operands agree, raises a finding when two committed operands are incompatible, and clears to top when a committed operand meets an unrefined one.
- **Aggregation.** The aggregate rule, whose behaviour is the measure's *combinability*. A genuine choice, treated below.

So a property chooses behaviour only at scalar transforms and at aggregation; the rest follows from the lattice.

## Two families of properties

Properties differ by where their transfer rules come from, and that difference is the whole of the structural/user-domain distinction. The engine does not branch on it.

- **Framework transfers** are theorems about SQL semantics, true in every project: a `JOIN` multiplies cardinality, `DISTINCT` introduces a key, `COALESCE(x, 0)` is non-null. Nullability, uniqueness, cardinality, grain, and ordering are the proven core, verified once.
- **User transfers** rest on declared signatures: whether `revenue * 0.9` preserves tax inclusion is what the author meant, which the framework cannot derive. Currency, tax inclusion, gross/net, and the other user-domain axes build from these, in an open catalog users extend.

A finding carries the assumptions in its derivation. One built only from core transfers is unconditional; one that passes through a user signature holds given that signature. What must be proved differs; the machinery does not. The framework proves its own rules; a user rule is correct as long as the author's declared signature behaves the way they say, and the runtime layer catches an inaccurate vouch empirically.

## Plan-shape independence

The walk must give the same annotation regardless of which equivalent query plan produced the SQL. This guarantee has three sources, one per family, and naming them is what keeps "the semiring is optional" from reading as "nothing buys plan-independence here."

- **Semiring distributivity.** For a counting or accumulating property carrying a `semiring`, distributivity of `×` over `+` is what makes equivalent relational expressions agree (Green, Karvounarakis, Tannen). The semiring laws are the obligation; when they hold, plan-independence is free.
- **Confluence homomorphism, no cross-column combine.** For a value-domain or idempotent column property there is no `×` at a join to distribute, because each output column traces to one input column. Plan-independence reduces to confluence being associative and commutative (it is the lattice join) and the scalar combines composing. There is nothing further to prove.
- **Key and functional-dependency propagation.** Uniqueness is key/FD propagation (Abiteboul, Hull, Vianu), not a semiring property, which is why it carries no `semiring`. Its `JOIN` combine reads the equijoin predicate, so it is not a binary operation on `K`. The technique is to normalize the equijoin into a column-equivalence and combine the quotiented key sets; the plan-independence theorem is the classical invariance of propagated keys under join reordering over the equijoin SPJ core. The detailed rules and proof live with the engine in [`column-level-lineage.md`](./column-level-lineage.md).

## Aggregation

The aggregate rule asks whether a measure's meaning survives a `GROUP BY`, and under what precondition. Three outcomes cover it: preserved, preserved under coherence, or cleared. The rule is two pieces so its soundness obligation stays checkable.

- A pure **`core`** (`AggFunc × Annotation -> Annotation`, with no dependency read). The core is what must **commute with confluence and cross**, so the single-pass walk gives the same annotation whether an aggregate sits above or below a `UNION ALL`. Because it is pure, it is discharged in isolation: property-tested directly for a value-domain property, and for a counting or accumulating property it is the semimodule homomorphism law (Amsterdamer, Deutch, Tannen) inherited once the property supplies its semimodule.
- An optional **coherence guard** that reads a functional dependency and clears to top where it fails (the mixed-currency `SUM`). Clearing to top commutes with confluence trivially, since top absorbs, which is exactly why factoring coherence out of the core lets the commutation law be stated over a pure function. The guard's plan-stability rests on the FD property it reads being itself plan-independent.

Aggregation is the one place the bare lattice does not force the rule, which is why it gets its own slot.

## Cross-property dependencies

A few properties read another's annotations to compute their own transfers: cardinality reads uniqueness to tell a fan-out join from a key-preserving one; a user-defined money type reads a functional dependency to decide whether a grouped sum keeps its currency. A property names the properties its transfers read in `depends_on`, the propagator evaluates those first, and a transfer reaches a dependency only through a read-only, typed channel.

Two obligations keep this sound.

- **Acyclic.** The `depends_on` graph is acyclic, so no pair of properties needs a joint fixpoint over a product lattice, and the walk stays single-pass.
- **Monotone in the dependency.** A transfer that reads a dependency is monotone in the dependency's value as well as its own input, so a dependency degrading toward top can only make the transfer more conservative, never let it claim more. A silent dependency reads as top, the most conservative input, so the absent case holds by construction.

## Determinism of the walk

The propagator carries an `Annotation`, a value plus two diagnostic bits (an `opacity` tag and an error-recovery `provisional` taint). Soundness and monotonicity are claims about the `value` only. The two bits are diagnostic metadata, outside the precision order, and they are deterministic functions of the walk: the lineage graph is a DAG visited in dependency-then-topological order, so each node's full annotation is fixed by its inputs. `provisional` is non-monotone along dataflow on purpose (it clears on a fresh consistent anchor, which is error recovery) and is read only to downgrade a finding's severity, never to license a more precise value or suppress a sound finding. So the value's soundness is independent of the taint.

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
