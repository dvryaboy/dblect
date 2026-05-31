# dblect design concepts: a digest

This document collects concepts and design decisions from the project's early architectural discussions that aren't currently documented elsewhere. It complements the flag user guide (which is task-oriented), the var inference spec (which is implementation-oriented), and the type theory tutorial (which is educational). It is intentionally a digest, not a tutorial: readers should expect terse coverage with pointers to fuller treatments where they exist.

## Architectural principles

### Two lattices, not one

Refinement information in dblect lives in two formally distinct lattices that compose. Mixing them into one calculus produces something hard to prove sound and hard for users to reason about.

The **structural lattice** covers cardinality bounds, uniqueness within keys, grain, freshness, and ordering. These propagate the same way in every project, because they're about how SQL operators move rows around: a join multiplies cardinality, a GROUP BY collapses it, a DISTINCT enforces uniqueness. The propagation rules are universal, and the framework can prove their soundness once. This lattice is conceptually adjacent to the functional dependency propagation tradition in classical relational theory.

The **user-domain lattice** covers tax inclusion, currency, gross/net, post-dedup status, timezone, and similar per-project semantic axes. These depend on project-specific vocabulary. The framework supplies a small set of composition rules (preserve, erase, drop-to-aggregate, branch-join) and the user declares the axes themselves.

Both lattices propagate together through the same SQL, but they remain formally separate. Structural soundness is a framework-level guarantee; user-domain soundness is conditional on the user's declared signatures being accurate. This keeps the framework's claims precise: we prove what we ship, we ask users to vouch for what they declare.

The composition rule between the two lattices is straightforward: structural refinements propagate independently of user-domain refinements at each operator, and vice versa. A JOIN's structural rule (cardinality multiplication) applies regardless of what user-domain axes are present on the joined columns; the user-domain rules apply column-wise without reference to the structural state. They commute, which is what makes the formal proof tractable.

For a longer treatment with worked examples, see the type theory tutorial. The proposed substrate realisation, where the distinction rides on a single `Soundness` tag (`PROVEN` / `VOUCHED`) over one propagation engine rather than two engines, is in [lineage-facts.md](lineage-facts.md); if that design is adopted this section gets reconciled to the tag framing.

### The propagation engine as shared substrate

The same operator signatures drive both the static type checker and the runtime PBT-style property tester. The static checker uses signatures to compute refinements at every SQL node and report contract mismatches. The runtime tester uses the same signatures to drive input generation: it knows what refinement each input must satisfy to exercise a given path, and it generates inputs accordingly.

This is a consistency check on the calculus design. If we found ourselves writing two different sets of operator semantics for static and runtime work, the calculus would have the wrong shape. The fact that one set suffices is evidence the design is on solid ground.

Practical implication: extending the framework with a new operator or a new refinement axis is a single piece of work, not two. The static and runtime paths inherit the extension automatically. This is significant cost amortization across the project's two halves.

### Adoption gradient: zero, partial, full investment

The framework gives useful output at every level of declaration investment. Three layers ship in v1:

- *Zero-declaration audit:* Static analyzer catches ordering hazards, unsafe `ROW_NUMBER() = 1` patterns, ambiguous tiebreakers in window functions; runtime executes models in dbt-duckdb to check replay-determinism and heuristic invariants. No semantic types involved. `dblect init` produces first findings end-to-end in under a minute on typical projects.
- *Typed critical chain:* Declare semantic types on the critical chain (revenue, customer_id, order_date). Run propagation. Most projects find at least one real bug within the first hour.
- *Focused contracts:* Add conservation and cardinality contracts on the few joins where fanout matters. The PBT runtime half engages with the intent catalog. Hours-to-days commitment.

A future layer (custom operator semantics for project-specific UDFs, for the small fraction of users with unusual domain operators that the framework's defaults can't capture) is deferred until v1 ships and demand surfaces. The v1 escape hatch for these cases is the `@contract.check` decorator plus `# noqa-fixture` annotations on regions the framework should treat as opaque.

No layer requires the next. A team that stops at the typed-critical-chain layer has a working semantic typecheck and never sees the words "lattice" or "operator signature." This gradient is the answer to the recurring tension between formal rigor underneath and practical adoption on top.

### Surface stays Pandera-shaped

Users declare semantic types, model contracts, and flags as Python classes that look like Pandera schemas. They don't write propagation rules, operator signatures, or refinement axioms by hand. The lattice and propagation machinery is internal to the framework.

This is a hard design constraint. Anywhere a future feature would require users to write something that looks like a type rule or a propagation signature in regular use, the design should be revisited. The escape hatch is the `@dblect.function` decorator for project-specific UDFs, which is the only place users encounter anything resembling a signature in v1.

YAML appears only as a compatibility surface for projects that want column tagging in `schema.yml`. The canonical declaration form is the Python class.

## Core design decisions

### Literals are opaque to refinement propagation

A line like `revenue * 0.9` could mean many things: a 10% fee adjustment that preserves tax and discount status, a currency conversion that transforms the meaning, a sampling factor with no semantic effect. The framework can't infer which, and trying to guess produces silent wrong answers.

The default behavior: literals erase the refinement of any column they touch arithmetically. The user gets a warning at the consumer. To preserve refinement, the user annotates the expression:

```sql
SELECT revenue * 0.9 AS discounted_revenue  -- dblect: discount(0.10)
```

This surfaces the design choice the user already made when writing the SQL, and produces an audit trail. The "revenue times 0.9 silently lost the tax-inclusion meaning" bug happens precisely because nobody thought about the 0.9 at the time of writing. Forcing annotation makes the thinking visible.

The annotation surface stays small. v1 supports `dblect: preserves`, `dblect: discount(N)`, `dblect: tax(rate)`, and `dblect: currency(from, to)`. Other operations get the opaque treatment.

### Window functions sit out of v1 propagation

Refinement propagation through window functions interacts with cardinality, ordering, and scope refinements simultaneously, and the prior art is thin. Adding them to the v1 calculus is substantial formal work for marginal demo value.

The v1 move: mark window function regions as type-erasing boundaries for refinement purposes, same treatment as UDFs and recursive CTEs. Re-annotation is required at the output of a window region. The separate ordering-hazard detector in the static analyser still flags dangerous patterns like `ROW_NUMBER() = 1` without ambiguous tiebreaks.

This is a scope decision, not a permanent exclusion. v2 or later can add window propagation once the v1 calculus is stable and the formal core has paper-proof-quality coverage of the operators it does handle.

### Formal core: tech-report scope, not POPL

The formal underpinnings of dblect are tractable. The operator language is small (filter, project, join, aggregate, case). The two lattices are simple. The soundness proofs are mechanical given the right setup. Five to fifteen pages of careful writing produces a tech-report-quality artifact that catches design errors before they're encoded in implementation and gives the project the technical authority an OSS framework needs.

A POPL-grade contribution would require novelty: a new proof technique, a new formalism, or a new theoretical result. dblect's formal core can rest on existing machinery from refinement types, type qualifiers, K-relations, and FD propagation, adapted to a new domain (SQL with user-declared refinements). That's a tech report or a workshop paper, not a top-tier conference submission.

The scoping matters because POPL-grade work would consume substantial effort we'd rather spend on implementation. The tech-report level is the right cost/value tradeoff for the project's needs.

### Macros are tractable when constrained by dbt

Macro expansion sounds open-ended in the abstract (templating languages can be Turing-complete) but is genuinely tractable in the dbt-specific case. The manifest is authoritative for the macro universe. dbt's Jinja environment is well-defined. Typical macros are simple text-substitution wrappers, with maybe 15% using internal control flow and 5% being exotic.

The implementation pattern for the macro-following layer: lookup from the manifest, lexical parameter substitution, recursive walk with depth limit, symbolic evaluation when call-site arguments are literals, graceful fallback to "opaque with reason" for cases that resist expansion. A few hundred lines of Python.

The meta-point: when a problem looks like a general open-ended one, check whether the specific instance is constrained by its environment. dbt's manifest and Jinja conventions constrain macro expansion enough to make it tractable. This pattern recurs throughout the project. SQL is constrained by sqlglot's normalization, flags are constrained by dbt's var system, contracts are constrained by manifest-known model boundaries. The framework leans on these constraints heavily.

## Positioning

### Compared to data-diff tools

Data-diff tools (Datafold and similar) compare two versions of a query against the same input data. dblect compares declared semantic guarantees against the SQL that's supposed to satisfy them. The outputs are different categories of evidence:

- Data-diff produces *quantitative deltas* against a fixed input snapshot.
- dblect produces *categorical root causes* and explores configuration space without depending on input data at all.

Data-diff is hard to beat for any case where the bug produces a visible data change against a stable baseline. Distribution shifts, row count changes, value drift. Most analytics bugs are in this category.

dblect uniquely covers cases data-diff structurally can't:

- *Bugs that don't manifest in current data.* Join fanout on input shapes production hasn't seen yet. New models with no baseline. Rare-customer behaviors that production data doesn't exercise.
- *Flag changes that don't trip current data.* When a flag's True branch isn't exercised by current customers, the value diff is empty until the configuration changes.
- *Values that coincide but meanings drift.* When pre-tax and post-tax happen to produce the same numeric values in your current data, diff sees nothing; semantic types catch the meaning shift.
- *Temporal and replay properties.* Idempotence, late-data tolerance, replay-class behaviors. These don't show up in two-snapshot comparison.
- *Cross-table semantic correlation.* "Contains_tax must hold consistently across these three related models." Diff tools treat tables independently.

The cleanest framing: data-diff tells you what changed; dblect tells you what contracts broke and explores configurations production hasn't reached yet. They compose. A team running both gets coverage neither tool alone provides.

For bugs both tools detect, dblect typically gives the better diagnosis. Data-diff reports "5,234 rows differ; revenue mean shifted 9.5%." dblect reports "the contains_tax refinement flipped from False to True; three downstream models declared the False version." Same bug, different category of output.

### Compared to declared-test approaches

dbt's own tests, dbt-utils, dbt-expectations, and Great Expectations occupy adjacent ground. They check declared properties on actual data. Useful, widely deployed, and somewhat overlapping with dblect's runtime PBT half.

The differences:

- *They check production data; dblect's PBT generates inputs systematically.* Production data carries the biases the production environment has; generated inputs explore shape space the engineer didn't think to seed.
- *They don't do static type propagation.* A test that asserts "revenue is positive" doesn't help when the meaning of "revenue" changes between models. dblect's static layer catches semantic drift at PR time without running any data.
- *They don't explore configuration space.* Tests run with whatever flag values the test environment has. Flag-world enumeration in dblect explores untested configurations.

The complementarity is similar to the data-diff case. dblect runs alongside dbt tests, with different specialties. The dblect runtime half overlaps more with existing test frameworks; the static and flag-world halves are the unique territory.

### Where dblect uniquely earns its keep

Three classes of bugs are where dblect is the right tool and nothing else cleanly covers the ground:

- Bugs that exist in shape space but not in current production data
- Bugs that exist in untested flag configurations
- Bugs whose diagnosis requires understanding semantic meaning, not just numeric difference

These aren't the majority of analytics bugs, but they're the highest-cost-when-missed and the longest-undetected. The positioning argument is "complement that catches what nothing else does," not "replacement for existing testing." Demo material should lead with cases data-diff structurally can't see, so the demo proves the right thing.

## Integration scope

### Why v1 is dbt vars only

The flag system in v1 covers `dbt var()` and `env_var()` references discovered through the manifest. Everything else is deferred.

The rationale: dbt vars are universal in dbt projects, the manifest gives complete discovery for free, and the implementation path (Jinja AST walking, type inference, world enumeration) is well-defined. Going broader in v1 would multiply integration complexity without proportional user value, since most users get most of their flag coverage from dbt vars anyway.

### What's deferred and why

*Per-entity flags from seed-based config tables.* The pattern where a `customer_config` seed carries flag values per row, and models join against it for per-customer behavior. Structurally different from global flags (worlds enumerate per entity, not globally) and deserves its own design pass.

*External flag platforms.* LaunchDarkly, Statsig, Unleash, OpenFeature. These have warehouse data exports that dblect can read, but each requires a platform-specific adapter. Shipped as adapters in later versions, on top of the v1 flag substrate.

*Cross-package flag inference.* Flags declared in one dbt package and referenced by another that doesn't import it. Requires manifest-spanning analysis that v1's per-project scope doesn't cover.

*Application-side flags.* Flags evaluated in producer code that change the shape of data written to the warehouse but never appear as columns there. dblect can't auto-discover these. Users can declare them manually if they want, with hints about which source columns are flag-dependent.

In every deferred case, the underlying flag machinery is identical. Adding integrations in later versions is shallow work because the abstraction (`SemanticFlag` with type, domain, and effect) is stable.

## Prior art map

Different traditions contribute different pieces to dblect's design. Understanding the map helps when reading the literature and when explaining the design to others.

*Refinement types* (Liquid Haskell, F*, Dafny). The general framework of types-with-predicates and the meta-theoretic pattern of trusted user-supplied axioms with proven framework rules. dblect uses the conceptual posture but not the SMT-decidable predicate machinery; our refinements are tags, not arithmetic facts.

*Type qualifiers* (CQual, FlowCaml). The closer prior art for tag-style refinements. Qualifier lattices, qualifier inference, operator signatures expressed as qualifier transformations. This is essentially what dblect's user-domain lattice is, adapted to SQL.

*Information flow* (Sabelfeld and Myers; the broader IF security literature). The deepest source for qualifier-system foundations. Noninterference and security-type-system techniques port directly to our setting with "tax-inclusive" in the role of "high security."

*SQL formal semantics* (HoTTSQL, Cosette). The K-relations framework provides a clean formal model for what SQL operators do. The equivalence-focused results of these papers are less directly portable; the semantic foundation underneath is what we reuse.

*Functional dependency propagation* (classical relational theory; Abiteboul, Hull, Vianu). The mathematical analogue for our structural lattice. Worked out formally in textbook depth, with decades of clean results to build on.

*Pandera and Pydantic.* The user-facing surface design. Class-based declarations, `Field()` for refinements, validation as a checkable contract. dblect imports this pattern directly.

The combined picture: dblect is a domain-specific type system whose constituent ideas are all standard, assembled in a way that's mildly novel for the analytics domain. The novelty is in the application, not in any individual piece of theory.

## Closing

This digest exists because a project's design conversation produces useful material that doesn't always fit into task-oriented documentation. The architectural principles, scoping decisions, positioning logic, and prior art map are all things future contributors and integrators will want to reference, even when no single user task requires them.

Updates to this document should follow the rule: if a discussion produces a generalizable insight or a load-bearing design decision, capture it here. If it's specific to a single user-facing surface or implementation, it belongs in the relevant guide or spec.
