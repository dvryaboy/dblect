# Config and flag worlds: compile-time configuration as facts

Status: design
Audience: engineers building the config discoverer ([#39](https://github.com/dvryaboy/dblect/issues/39)), the compile-value discoverer and flag-world bridge ([#40](https://github.com/dvryaboy/dblect/issues/40)), or the var-inference layer ([`var-inference-spec.md`](./var-inference-spec.md)). It assumes the facts substrate from [`lineage-facts.md`](./lineage-facts.md) (how a declaration becomes a grounded value that enters the walk) and the propagation calculus from [`propagation-soundness.md`](./propagation-soundness.md). The field survey of dbt configuration this rests on is [`research/dbt-config-patterns.md`](./research/dbt-config-patterns.md).

This doc covers one problem and the deep fork inside it: how dbt's compile-time configuration (model `config` keys, `var()` / `env_var()` values, and the environment dispatch around `target` and `is_incremental()`) becomes facts the propagator can read, and what it takes to check a project across more than the one configuration a single manifest happens to capture.

## Motivation: two issues, one seam

[`lineage-facts.md`](./lineage-facts.md) already names two forward-looking discoverers and ships their value types. The substrate carries `CompileValue` provenance with a `CompileOrigin` (`DBT_VAR`, `ENV_VAR`, `DBT_CONFIG`, `COMPUTED`) and a `WorldRef`, and states the posture: the flag layer fixes one world per propagation run, discoverers emit their facts under it, and a difference *between* worlds is the flag-world analysis. What is missing is the two discoverers themselves and the bridge from a flag declaration to a per-world fact.

- **Config-derived facts** ([#39](https://github.com/dvryaboy/dblect/issues/39)). Read `node.config` keys a property cares about (`materialized`, `incremental_strategy`, `unique_key`) and produce relation facts (`origin=DBT_CONFIG`).
- **Compile-resolved values** ([#40](https://github.com/dvryaboy/dblect/issues/40)). Produce `CompileValue` facts where a refinement's `affects` clause resolves to a single value under the chosen world. Where the value is statically enumerable (`DBT_VAR`, `ENV_VAR`) the flag layer enumerates worlds over it; where it is computed opaquely (`COMPUTED`) the flag layer sees the single resolved value as one world. This is the bridge to the flag-world enumerator described in [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md).

These look like two small discoverers. They are the surface of a deeper question that the rest of this doc is about: **the manifest we analyze is the compilation of exactly one configuration, and the configuration itself has been erased by the time we see the SQL.** Getting configuration into the facts substrate means deciding where worlds come from and how we obtain the per-world SQL without re-deriving the whole project once per world.

## The parsing reality: configuration is invisible in what we parse

dblect analyzes **compiled SQL**. `Manifest.Node.analysis_sql` returns `compiled_code`, and every analysis path reads it: the lineage builder, the nullability property, the SQL detectors, the relation walk. The one place the on-disk template (`raw_code`) is touched is the suppression scanner, which text-matches `-- noqa-fixture:` comments and never parses. So the pipeline is `compiled SQL -> sqlglot AST -> detectors and lineage`, and by the time sqlglot sees the SQL, dbt's Jinja runtime has already run.

That timing is the whole problem. After Jinja runs:

- A **value-substitution** var has collapsed to a literal. `where region = '{{ var("region") }}'` is now `where region = 'US'`, a constant indistinguishable from one a developer typed by hand. The var's identity is gone, and with it any record that this value is configurable.
- A **control-flow** var, `target` dispatch, or `is_incremental()` has already had **one branch chosen**. `{% if is_incremental() %} ... {% endif %}` is either present or absent in the artifact; the other branch does not exist in it at all.

So the compiled manifest is not "the model." It is *one compilation of the model under whatever configuration produced this manifest*. The current static analyzer, which is sound and useful for structural hazards, is silently analyzing a single world of every model that branches on configuration, and any hazard (or any clean bill) in the unexercised branch is invisible to it. Naming that is half the motivation for this work: today an incremental model's first-run branch and its steady-state branch are different SQL, and the audit sees only the one dbt last compiled.

This splits the design cleanly and sets up everything below:

- **The substrate's "one world per run" posture is automatically satisfied by a manifest.** A manifest *is* a world. So the config discoverer, which reads values already resolved in that manifest, needs no enumeration machinery to do its job in the current world. This is why [#39](https://github.com/dvryaboy/dblect/issues/39) was separable and shipped on its own in [#82](https://github.com/dvryaboy/dblect/pull/82).
- **Reasoning across worlds needs a second front end** that sees variability before Jinja erases it. That is the Jinja source, and parsing it is exactly what [`var-inference-spec.md`](./var-inference-spec.md) proposes. The project already implies a two-front-end architecture (sqlglot over compiled SQL for structure, a Jinja AST over source for variability); the work here is to connect them.

## Theory: this is family-based static analysis

A dbt project with vars and `{% if %}` blocks is a configurable program: one code base that compiles to many programs, one per configuration. Checking "does my contract hold under every configuration of my flags" is a known shape in the programming-languages literature, and naming it correctly buys both a vocabulary and a set of proven techniques.

### Product-based, feature-based, family-based

The consensus taxonomy is Thüm, Apel, Kästner, Schaefer, and Saake's survey (ACM Computing Surveys 47(1), Article 6, 2014). It classifies analyses of a configurable system by what they run over:

- **Product-based**: generate each configured product and analyze it with an off-the-shelf analyzer, possibly over a sample of configurations. Simple and reuses existing tools; cost grows with the number of products, and a sample is unsound for the configurations it skips.
- **Feature-based**: analyze each feature's code in isolation. Cannot see feature interactions on its own.
- **Family-based**: analyze the whole code base once, parameterized by configuration, using variability knowledge to cover all valid products in a single pass. This is the category that "lifted" and "variability-aware" analyses belong to.

dblect's flag-world analysis, as pitched in [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md) ("enumerates every configuration and propagates types through your SQL in each one, reports any world in which a declared contract fails"), is described as product-based: run the per-world typecheck, repeat. That is a perfectly good place to start and is the honest baseline. The literature's contribution is showing how to move from there toward family-based when the configuration space grows.

### The lifted lattice and the cost spectrum

Brabrand, Ribeiro, Tolêdo, and Borba ("Intraprocedural Dataflow Analysis for Software Product Lines", AOSD 2012, extended in TAOSD with Winther) lay out the spectrum directly, as strategies from feature-oblivious brute force to aggressive sharing:

- analyze each configuration separately (the product-based baseline),
- analyze configurations consecutively but feature-sensitively,
- analyze them simultaneously, caching shared intermediate results,
- share the lattice values across configurations with a compact representation,
- split configuration sets lazily, only where the analysis actually disagrees.

The unifying construction is the **lifted lattice**: from a base lattice `L` for the single-program analysis, build the lattice of functions from configurations to `L`, and lift the transfer functions pointwise. Many configurations map to the same value, so the shared representations win. Midtgaard, Dimovski, Brabrand, and Wąsowski ("Systematic Derivation of Correct Variability-Aware Program Analyses", Science of Computer Programming 105, 2015) put this on an abstract-interpretation footing: lift each step of the analysis as a sound abstraction and the variability-aware analysis is correct by construction, rather than designed ad hoc and proved sound afterward.

This maps onto dblect's substrate almost directly. The propagator already carries, per node, an `Annotation[K]` over a property's lattice. A family-based version carries a *world-indexed* `Annotation`, a function from `WorldRef` to `K`. The structural walk is shared; only the grounding and the values vary by world. dblect's `WorldRef` is exactly the "configuration" of the lifted lattice.

### Presence conditions are a provenance semiring

dblect's propagator is already built on the commutative-semiring framing of Green, Karvounarakis, and Tannen ("Provenance Semirings", PODS 2007), with the aggregate extension from Amsterdamer, Deutch, and Tannen (PODS 2011). The flag-world generalization fits the same algebra rather than adding a parallel one. SPLLIFT (Bodden, Tolêdo, Ribeiro, Brabrand, Borba, and Mezini, "Statically Analyzing Software Product Lines in Minutes Instead of Years", PLDI 2013) lifts any IFDS dataflow analysis to a product line by reducing it to IDE and labeling each conditional edge with a **presence condition**, a boolean formula over configuration options, represented as a minimized BDD. The value flowing with a fact becomes the set of configurations for which the fact holds, and the lift is transparent to the underlying analysis.

A presence condition is a provenance annotation over the configuration space: "this fact holds in exactly these worlds." For dblect this says the world-indexed annotation is not a foreign construct bolted onto the semiring engine; it *is* a semiring value whose carrier is the world space. When flags carry numeric or enum domains rather than booleans, the BDD generalizes to the decision-tree lifted domain of Dimovski, Apel, and Legay ("A Decision Tree Lifted Domain for Analyzing Program Families with Numerical Features", FASE 2021), where inner nodes are decisions over feature expressions and leaves hold a base-domain value, sharing across configuration regions that agree. dblect's `WorldRef` enumeration is the explicit, un-shared form of the same object; a decision tree is its compressed form.

### Variability-aware parsing: preserving the branches

Family-based analysis of a control-flow var needs the branches, and the branches are erased in compiled SQL. The C product-line community solved the analogous problem for `#ifdef`. TypeChef (Kästner, Giarrusso, Rendel, Erdweg, Ostermann, and Berger, "Variability-Aware Parsing in the Presence of Lexical Macros and Conditional Compilation", OOPSLA 2011) parses unpreprocessed C, with conditionals and macros still in place, into a single AST that preserves variability as **choice nodes** guarded by presence conditions, rather than committing to one configuration. SuperC (Gazzillo and Grimm, "SuperC: Parsing All of C by Taming the Preprocessor", PLDI 2012) gives the implementable engine: **fork-merge LR parsing** forks the parser at a static conditional, runs a subparser per branch carrying that branch's presence condition, and merges them when they reconverge to the same parser state after the conditional ends. The formal calculus of this kind of variation is the choice calculus of Erwig and Walkingshaw ("The Choice Calculus: A Representation for Software Variation", ACM TOSEM 2011).

The analogy is tight. `{% if var('x') %}` is dbt's `#ifdef`; `{% for m in var('methods') %}` is a bounded expansion; Jinja interpolation straddling SQL token boundaries (`{{ var('schema') }}.users`) is the same lexical-versus-grammar hazard TypeChef warns about, where a naive lex-then-parse pipeline mishandles variation that crosses tokens. A variation-preserving dbt front end would parse the Jinja source into a SQL AST whose var-dependent regions are choice nodes, and the lifted propagator would walk it once.

### Taming the blow-up

One lifted run is still, worst case, proportional to the number of configurations. Dimovski, Brabrand, and Wąsowski ("Variability Abstractions: Trading Precision for Speed in Family-Based Analyses", ECOOP 2015; family-based model checking in STTT 2017) give the principled escape hatch: a **variability abstraction** is a Galois connection over the configuration space that deliberately collapses feature distinctions, so the abstracted analysis soundly over-approximates the per-configuration results and can be refined where precision is needed. For dblect this is the rigorous version of "per-contract enumeration": a contract whose property is moved by two of forty flags should be checked over the four worlds of those two, not the full product, and the abstraction that drops the other thirty-eight is sound by construction. "Moved by" is the operative relation, and it is computed from lineage rather than read off the contract's text. A flag counts if it lies in the contracted node's influence cone (defined under "Taming the world space" below), so a flag the contract never mentions but that reaches it through the dataflow is correctly kept, and one that cannot reach it is correctly dropped. The thirty-eight that drop out are the ones provably outside that cone, which is why the count of flags a contract actually pays for is a transitive property of the DAG and not the set of names appearing in the model.

### Foundations and the family-of-programs idea

The substrate is already a parameterized abstract interpretation in the sense of Cousot and Cousot ("Abstract Interpretation", POPL 1977; "Systematic Design of Program Analysis Frameworks", POPL 1979): one `propagate` function, instantiated per property by its lattice and transfer catalogs. ASTRÉE (Blanchet, Cousot, Cousot, Mauborgne, and Miné, "A Static Analyzer for Large Safety-Critical Software", 2003) is the worked precedent for designing a parametrizable analyzer once and adapting it to a *family* of related programs, which is structurally what a dbt project's worlds are: a family that shares almost all of its code.

## What the substrate already provides

The data model for this work is in place, and the config discoverer shipped in [#82](https://github.com/dvryaboy/dblect/pull/82); the compile-value discoverer and the enumerator are not yet.

- **`CompileValue` provenance** carries `origin: CompileOrigin` and `world: WorldRef`. `world` is never absent: a compile value is ground truth in exactly the world the flag layer fixed. `origin` records how the value is produced and whether the framework can auto-discover it. The doc comment is already explicit that enumerability is a function of the flag's declared-or-inferred domain, not of `origin` alone (a `COMPUTED` value with a user-declared finite domain is enumerable; a `DBT_VAR` with an open domain is not).
- **`WorldRef`** is `frozenset[tuple[str, Hashable]]` of assignments, hashable with value equality, opaque to the substrate in meaning. Facts bucket by world equality, so resolution within a world is ordinary and order-independent.
- **The propagator** runs one world per run, accumulating annotations into a store. Within a world a `CompileValue` fact has the same standing as a native constraint or a user assertion, and `resolve` folds a scope's facts with the lattice meet regardless of provenance.
- **Conditional facts and the predicate engine** ([`predicate.py`](../../src/dblect/lineage/predicate.py)) already exist for the `where`-filtered case, which the env and target dispatch patterns will reuse.
- **var-inference** ([`var-inference-spec.md`](./var-inference-spec.md)) specifies the Jinja-source front end: discover every `var()` and `env_var()`, infer type and domain, emit `VarUsage` records carrying the syntactic context (truthy test, equality, in-set, arithmetic, SQL-literal position) and source location. This is the second front end the cross-world work depends on, and its `UsageContext` is exactly the value-substitution-versus-control-flow signal.

## A taxonomy of worlds

From the dbt field survey ([`research/dbt-config-patterns.md`](./research/dbt-config-patterns.md)), the configuration mechanisms divide by what they do to the compiled SQL.

**Value-substitution worlds.** The configuration renders as a literal; the compiled SQL shape is invariant across worlds and only a leaf value differs. `var()` in a predicate or `LIMIT`, `env_var()` for a schema or database, `target.schema` in a name. For these, the structure in a single manifest is the structure in every world, so a family-based analysis can share the entire walk and vary only the leaf grounding. This is the tractable, high-value case.

**Control-flow worlds.** The configuration gates Jinja branching, so the compiled SQL is structurally different across worlds. `var()` in `{% if %}` or `{% for %}`, `env_var()` feeding a `config` key, `target.name` in `{% if %}`, and `is_incremental()`. A single manifest contains exactly one branch; the others are not recoverable from it.

Two control-flow axes deserve emphasis because they exist whether or not anyone declares a `DomainFlag`:

- **`is_incremental()`** compiles the same model two ways. On the first run (target absent, or `--full-refresh`) the incremental filter is absent and the model builds over all rows; on a steady-state run the `{% if is_incremental() %}` filter is present. The dbt docs require the SQL to be valid both ways, so both worlds are reachable by construction, and the effect is larger than the SQL diff suggests because dbt wraps the same SELECT in different DML (CREATE-AS versus MERGE / DELETE+INSERT / INSERT).
- **`target`** dispatch (`{% if target.name == 'dev' %}`) is structurally a control-flow var whose reachable values are closed and known from `profiles.yml`, though which value fires depends on an environment outside the repo.

The current audit sees one world of each of these. That is the soundness gap this work closes, and it is worth measuring rather than asserting (see "Coverage as a first-class output").

## The central fork: where worlds come from, and how we get per-world SQL

Value-substitution worlds and control-flow worlds need different machinery. The value case is cheap and the control case is the hard fork.

### Value-substitution worlds: lift over one manifest

The compiled SQL shape is world-invariant, so the structural walk runs once and the leaves are re-grounded per world. The only missing ingredient is re-attaching var identity to the literals the walk sees, which var-inference supplies: a `VarUsage` in SQL-literal position pins a source location and a var name, and the value present in the current manifest is that var's value in the current world. Enumerating the var's domain produces the other worlds' leaf values without recompiling anything. This is the family-based "share the walk, index the leaves by world" pattern in its simplest form, and it is the first real cross-world analysis dblect can offer.

### Control-flow worlds: three strategies

**Re-compile per world (product-based).** Invoke `dbt compile` once per world with the appropriate `--vars`, `--target`, or `--full-refresh`, collect one manifest per world, and run the existing analysis on each. It is sound and reuses the entire pipeline unchanged, which makes it the honest baseline. Its cost is the problem: a naive sweep grows with the *product* of all flag domains and re-runs a live dbt for each, so it is exponential in flag count and unusable as the general answer. It survives as a strategy only once the world space is factored (see "Taming the world space" below), which collapses the product to a sum of small per-cluster sweeps, and once compilations that produce identical SQL for a model are deduplicated. Treated that way it is a sound bridge for the always-present control-flow axes; treated as a full matrix it is the thing to avoid.

**Variability-aware compilation (family-based, the north star).** Build a variation-preserving Jinja front end (the TypeChef and SuperC route): parse the source, keep `{% if var %}` and bounded `{% for %}` as choice nodes carrying presence conditions, and produce a variational compiled SQL where var-dependent regions are guarded. Lift the property propagator over it (the SPLLIFT route): facts carry presence conditions, and one walk computes every world, sharing all configuration-invariant structure. This is the scalable endgame and the right long-term target, and it is a substantial build with the token-straddling footgun TypeChef documents. It does not need a live dbt at analysis time, which is its other major advantage over re-compilation.

**Bounded re-compilation for the always-present axes.** `is_incremental()` has exactly two states and `target` has a small closed set, so even before a general enumerator exists, compiling those specific worlds (two for incremental, one per target) closes the highest-frequency control-flow gap at a fixed, small cost. This is product-based but with a tiny, known product, and it is independently valuable.

### Recommendation and how it maps to the spectrum

Stage the work so each step ships something correct and the expensive machinery is gated behind demonstrated need.

1. **Config discoverer** ([#39](https://github.com/dvryaboy/dblect/issues/39)). No worlds. Reads the single manifest we already parse. Detailed below; the `unique_key` and `incremental_strategy` mapping is a correct, immediate strengthening of the uniqueness property.
2. **Value-substitution flag worlds** ([#40](https://github.com/dvryaboy/dblect/issues/40), the tractable half). Depends on var-inference for var-to-literal mapping. The bridge lowers `affects` to `CompileValue` facts per world; the walk is shared and only leaves vary. This is the first cross-world contract check.
3. **Bounded re-compilation** for `is_incremental()` and `target`. Closes the always-present control-flow gap at small fixed cost.
4. **General control-flow enumeration via re-compilation** ([#40](https://github.com/dvryaboy/dblect/issues/40) plus the enumerator). Product-based, gated by a flag-count budget and scoped per contract.
5. **Variability-aware compilation** when the configuration space outgrows re-compilation. The family-based endgame.

In the survey's vocabulary this is a deliberate climb from product-based (steps 3 and 4) to family-based (steps 2 and 5), with per-contract scoping (step 4) as the first variability abstraction and the lifted-lattice sharing (steps 2 and 5) as the destination. The substrate's existing semiring engine is what makes the climb continuous rather than a rewrite: each step changes what grounds a leaf and how worlds are indexed, not the propagation calculus.

## Taming the world space: the DAG factors it for us

The product of all flag domains is the wrong cost model, and avoiding it is not an optimization to bolt on later; it is what makes any of the strategies above tractable. The full Cartesian product over flags assumes every flag can interact with every other flag, which a dbt project's structure makes plainly false. Most flags are local: they influence one corner of the DAG and never meet most other flags downstream. Enumerating their combinations together computes answers that cannot differ.

The principle is the one the feature-interaction literature rests on: **only flags that interact need to be enumerated jointly.** Two flags interact, for the purpose of a given check, only if their effects can both reach the same checked node. dblect is unusually well-placed to compute this, because the thing that decides interaction is exactly the lineage it already computes.

### Interaction is reachability in the lineage graph

A contract is checked at a node (a column or relation with a declared value). Whether that contract holds is a function of the flags whose responsive code lies upstream of it, and of no others. The influence cone that captures "upstream of it" is the provenance that determines the property under check, which differs by property: where-provenance (the source columns whose values feed the node) for value and domain contracts, how-provenance (the multiplicity semiring dblect already carries) for uniqueness and cardinality, and the nullability-lattice flow (which tracks join shape and filters) for nullity. Intersecting the node's cone with the responsive scopes of each flag yields the node's **flag-influence set**: the only flags that can change whether this contract holds. A flag absent from the set cannot move the property, so it cannot break the contract, so its values never need to be varied while checking it.

This factors the world space. Build the flag-influence set for every checked node, then group flags into **interaction clusters** by the transitive closure of "appears together in some node's influence set." Flags in different clusters are independent: no checked node depends on flags from two clusters at once, so the worlds of one cluster are orthogonal to the worlds of another. The space dblect must explore is the **disjoint union of the per-cluster products**, not the product of everything. A project with twenty boolean flags that fall into clusters of size at most three costs at most a handful of clusters times eight worlds each, a sum in the dozens, rather than a million. The blow-up is bounded by the largest interaction cluster, which is a local property of how entangled the modeling is, not by the global flag count.

This is the same move as SPLLIFT's presence conditions (a fact carries only the flags it actually depends on) and as the variability abstractions of Dimovski and colleagues (soundly collapse configurations a check cannot distinguish), specialized to the asset dblect already has. The lineage graph *is* the feature-interaction model, computed rather than declared.

### Compilation sharing falls out of the same fact

The factoring also cuts the compilation cost, which is what makes re-compilation survivable for the control-flow axes. A flag that does not textually appear in a model's Jinja produces byte-identical compiled SQL for that model across all its values, so that model is compiled once for the whole flag and the result is shared. Per model, the only worlds that yield distinct SQL are the assignments of the flags that actually appear in it, which is typically one or two. So the set of *distinct compilations* the analysis needs is far smaller than the world count: it is the union over models of each model's local flag assignments, deduplicated by resulting SQL. Re-compilation done this way compiles a near-minimal set of artifacts rather than one per global world.

### Where the factoring needs care

Independence is sound only against the influence sets as computed, and two cases stretch them:

- **Structure-changing flags.** A flag on `enabled`, on a `{% if %}` that adds a join, or on a `ref` makes the lineage graph itself world-dependent, so the influence sets it feeds are not fixed. The conservative and sound treatment is to compute influence over the union of the structures such a flag induces (a node is influenced if it is reachable in *any* world of the structure-changing flag), which over-approximates interaction and therefore never wrongly declares two interacting flags independent. It can over-cluster, which costs worlds, never soundness.
- **Property-dependence beyond value flow.** The cone has to be the provenance of the *property* under check, because the properties dblect verifies are not all functions of value flow, and a where-provenance cone applied uniformly would silently drop the flags that move the others. An upstream `incremental_strategy` flag of `merge`-with-key versus `append` decides whether that model dedups (the [#39](https://github.com/dvryaboy/dblect/issues/39) mapping), yet a downstream model that `ref`s it compiles to byte-identical SQL either way, since the `ref` resolves to the same relation name. The downstream column's where-provenance is unchanged across the two worlds, so a value-flow cone would declare the flag irrelevant, while a downstream uniqueness or cardinality contract genuinely flips with it. Nullity has the same shape when an upstream INNER versus LEFT join injects nulls into a column whose value-provenance is identical. Taking the cone over how-provenance for multiplicity properties and over the nullability flow for nullity keeps these dependencies in the influence set. Both substrates already exist, so this is choosing the right cone per property rather than adding an analysis.
- **Cross-cluster contracts.** A single contract whose node genuinely depends on flags from what looked like two clusters merges them; that is the clustering working as intended, not a failure. The cluster sizes are an output worth reporting, since a surprisingly large cluster is itself a signal that a model is entangling configuration that the modeler may have thought independent.

The honest framing for the cost section: the worst case is exponential in the size of the largest interaction cluster, and the design's job is to keep clusters small by computing them precisely from lineage and reporting them when they grow. Per-contract enumeration and DAG factoring are the same idea seen from the contract and from the flag; together they are why the strategies above are tractable rather than aspirational.

### The empirical fork: which way a real project tips us

The choice between deduplicated re-compilation and variability-aware compilation is not one to settle in the abstract. It is a property of the projects we point dblect at, and the same lineage machinery that factors the world space also measures which regime a project lives in. The standing strategy is deduplicated re-compilation; variability-aware compilation earns its place only where a project's own shape shows that strategy turning back into a full matrix. Five seams carry that signal, and each can be read off a project before any of the heavy machinery is built, which is what makes this measurable against a real codebase rather than a guess.

- **Distinct compilations per model.** Compile each model across the assignments of the flags that textually reach it and deduplicate by resulting SQL. If almost every model collapses to one or two distinct SQL strings, deduplicated re-compilation stays cheap no matter how many flags the project declares, and the lifted front end is a luxury. If a tail of models fans out into many SQL variants that do not deduplicate, that tail is exactly the case lifting is for, and its size is the budget that justifies the build.
- **Largest interaction cluster.** Build the flag-influence sets and take the transitive closure. Clusters that stay small (a handful of flags) keep the per-cluster product in the dozens and make the sum-of-clusters cost a non-issue. A cluster that grows with the global flag count means a model is entangling configuration the modeler likely thought independent, and the honest responses are to report it, to recommend a refactor, or to lift that one cluster rather than enumerate it.
- **Control-flow share of the flag population.** Separate the flags that only substitute values (they collapse to literals under a shared walk and need no recompile) from the flags that steer `{% if %}`, `is_incremental()`, or `target` (they change the compiled SQL). A project dominated by value-substitution flags barely exercises the expensive axis. A project where many flags are control-flow and concentrated in a few models is where re-compilation cost gathers, and where lifting pays back first.
- **Hermeticity of compilation.** Check whether compiling a world is self-contained or reaches a warehouse through introspective macros (`run_query`, relation-existence checks, catalog lookups). Hermetic compilation fans out freely across worlds and keeps re-compilation comfortable. Compilation that needs a live connection per world is operationally heavy to sweep, which raises the value of lifting precisely because the lifted analysis needs no live dbt once the variational SQL exists.
- **Branch-point density in single models.** The structural signature that only lifting handles well is one model carrying several interacting conditionals whose combinations each yield distinct SQL, the kitchen-sink staging model or a dynamic-pivot macro. Counting interacting branch points per model locates these directly. A project with none can defer lifting indefinitely; a project with a meaningful set of them has found the one workload where deduplicated product degrades to the full matrix.

Read together, these decide the fork: deduplicated re-compilation as the standing strategy, bounded re-compilation for the always-present axes, and variability-aware compilation scoped to the specific models and clusters where the first two seams show the product becoming a matrix. The value of measuring is that it spends the lifted-analysis build on the part of a real project that needs it rather than on the project as a whole.

## The config discoverer (#39)

Config is the easy issue precisely because config values live in the manifest we already read, fixed in the current world. The discoverer is relation-scoped (config is per-model) and emits `CompileValue(origin=DBT_CONFIG, world=current)` facts. This shipped in [#82](https://github.com/dvryaboy/dblect/pull/82) as the worked example below; the rest of this section is the design it realizes and the keys still to adopt.

### Generic plumbing, per-property interpretation

The reading is centralized and the interpretation is per-property, matching the issue's note that "concrete per-key mappings land as detectors adopt them." A `ConfigDiscoverer` reads `node.config` and consults a registry of interest entries. Each entry names the config key or keys it reads, the property it grounds, and a pure interpretation function from the relevant config slice (plus the adapter, for defaults) to a fact value or `None`. Returning `None` is silence: a config that does not establish the property grounds nothing, the same "absence is silence" the rest of the substrate observes. The function is pure and total within its slice, so per-discoverer property-based testing applies unchanged.

This keeps config semantics next to the property that understands them. The uniqueness property knows what `unique_key` and `incremental_strategy` mean together; the discoverer framework does not need to. [#82](https://github.com/dvryaboy/dblect/pull/82) shipped this per-property form first: a `config_key_discoverer` reads a typed `ModelConfig` slice of `node.config` and grounds the uniqueness key directly, wired into `uniqueness_property` beside the declared-key discoverers. The centralized registry of interest entries is the generalization to reach for as more keys are adopted.

### Worked example: `unique_key` x `incremental_strategy`

The headline mapping, and the one that makes [#39](https://github.com/dvryaboy/dblect/issues/39) worth doing on its own, is candidate-key grounding for the existing uniqueness property. The dbt semantics are precise and easy to get wrong (see the dedup table in [`research/dbt-config-patterns.md`](./research/dbt-config-patterns.md)):

- `merge` with a `unique_key`, or `delete+insert`: the write deduplicates on the key. The output relation is unique on it.
- `append`, or `merge` without a `unique_key`: no deduplication. Setting `unique_key` alone enforces nothing.
- `insert_overwrite`: partition-level replacement, ignores `unique_key`.
- the default strategy is adapter-dependent (Snowflake and BigQuery default to `merge`; Postgres and Redshift to `delete+insert` once a `unique_key` is set; Spark to `append`), so the interpretation must read the adapter. These per-adapter defaults and the enforcement flags now live in the `AdapterProfile` registry ([#83](https://github.com/dvryaboy/dblect/pull/83)), resolved once from the manifest's adapter.

The interpretation emits a candidate-key fact **only** when the pair enforces dedup, and stays silent otherwise. A `unique_key` under `append` produces no fact, which is the correct and important behavior: the project looks like it declared a key, but nothing enforces it, so claiming uniqueness would plant a wrong fact and a wrong silence downstream.

This grounding has a stronger standing than a typical asserted fact. A `unique` data test asserts the SELECT produces unique rows; a `merge` materialization *enforces* uniqueness at write time regardless of what the SELECT produces. So the config-derived key is closer to an enforced constraint than to an assertion, and the propagator can treat it as ground truth on the output relation. One nuance keeps it honest: `merge` deduplicates incoming rows against existing rows, and behavior when a *single batch* contains duplicate keys is adapter-dependent (some error, some pick arbitrarily). So the config key is a strong guarantee across runs with a residual single-batch hazard, which is itself a detector opportunity (a SELECT that can produce duplicate keys feeding a `merge` is the relation-level analog of join fan-out). The discoverer grounds the key; the hazard is a separate finding.

### Other config interests, as detectors adopt them

Sketched, to be filled in by their consumers rather than up front:

- `materialized=ephemeral`: the model has no independent relation; its SQL inlines as a CTE into consumers. Lineage and relation facts should fold into the consumer rather than attach to a standalone relation.
- `enabled`: a disabled model is absent from the manifest, so its presence in the manifest means it is enabled in the current world. Enumerating `enabled` driven by a var is a control-flow world (node existence varies), which belongs to the flag layer, not the config discoverer.
- `on_schema_change`: relevant once control-flow worlds make the column set itself world-dependent, since it governs what happens when a model's columns change between runs.

## The compile-value discoverer and flag-world bridge (#40)

The bridge is the discoverer that, given a chosen world, lowers flag declarations to `CompileValue` facts. The enumerator that chooses worlds and compares across them is separate and stays out of scope here, exactly as [`lineage-facts.md`](./lineage-facts.md) draws the line ("this module supplies values inside a world; the flag layer chooses worlds").

### Lowering `affects` to a fact

A `DomainFlag` carries an `affects` clause mapping the flag's value to a refinement axis value (`RefinementEffect(target=Revenue.contains_tax, value_when_true=True, ...)`), as specified in [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md). Given a `WorldRef` that fixes the flag's value, the bridge evaluates `affects` at that value to get one axis value, and emits a `CompileValue` fact setting that axis at each responsive scope. The `origin` follows the flag's link (`DBT_VAR` or `ENV_VAR`).

The phrase from [#40](https://github.com/dvryaboy/dblect/issues/40), "a single value under the chosen world," is the load-bearing one. Because the world fixes the flag, `affects` produces exactly one value, so the fact is concrete (`Opacity.CONCRETE`), never a disjunction. The disjunction across flag values lives in the enumerator, across worlds, never inside the substrate. This is what keeps `resolve` ordinary: every fact in a bucket shares one world, and a scope's value in a world is a single point in the lattice.

`OpaqueEffect` lowers to a declared opaque opt-out on the affected axis (a top-`EXPLICIT` declared annotation, synthesized by the grounding builder rather than stored as a top-valued fact), which flows silently. This is the escape hatch for a flag whose effect the type system cannot express, and it composes with the seam diagnostic exactly as a `meta.dblect.opaque` marker would.

Axis-level composition (a flag that sets one axis of a multi-axis type, meeting a user contract that pins several axes) is the domain-type algebra's job, not the bridge's. The bridge emits a fact on the one axis `affects` targets; [`domain-type-algebra.md`](./domain-type-algebra.md) defines how per-axis claims fold at a scope.

### The COMPUTED case

A flag whose value is not statically enumerable (a macro that queries the warehouse, an exotic Jinja pattern var-inference declines to follow) is `origin=COMPUTED`. The enumerator cannot fan out worlds over it, so it uses the single value resolved in the actual manifest as one world, and the bridge lowers `affects` at that value. This is degrade-not-lie at the world level: an opaque flag becomes one honest world rather than a wrong claim or a crash, and the coverage report records that its domain was not enumerated.

### The responsiveness problem: which scopes does a global flag ground?

This is the genuinely hard sub-problem and the one most worth getting right, because a wrong answer plants wrong facts, which the substrate's first commitment ("facts must be rock-solid") forbids.

A flag's `affects` targets a *type's axis* (`Revenue.contains_tax`), but a fact attaches to a *scope* (a column or relation). So the bridge must decide which scopes a global flag grounds. [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md) phrases the intent as "any Revenue column produced by code that responds to this flag," which is a usage statement, not purely a type statement. Three readings, from coarse to precise:

- **Type-directed.** Every scope declared as the target type gets the axis set by the flag in this world. Simple, and defensible *as the flag author's own declaration*: writing `affects` targeting `Revenue.contains_tax` is a claim that this flag governs that axis for that type project-wide. The danger is real for user-domain axes: if a Revenue column is computed by flag-blind code, SQL inference usually cannot refute a user-domain axis (it grounds top), so a wrong type-directed fact flows unchallenged. Soundness here rests entirely on the author's project-wide claim being accurate, with no backstop.
- **Model-responsiveness (recommended v1).** A scope declared as the target type, *in a model that actually reads the controlling var*, gets the fact. Var-inference already produces per-model var usage, so this needs no column-level mapping. It narrows the claim to models that genuinely participate in the flag, which is where the author's "responds to this flag" intent actually lands, and it keeps the blast radius of an inaccurate flag declaration to responsive models. Scopes in flag-blind models stay ungrounded by the flag, which is "absence is silence" applied correctly.
- **Lineage-directed (precision refinement).** Only scopes whose lineage passes through the var's usage site get the fact. This is the most faithful reading of "produced by responsive code," and the most expensive: it requires joining a var's usage location (from var-inference over Jinja source) to a lineage column (from the compiled-SQL graph), across the two front ends the parsing split keeps apart. It is the right long-term target and a real cost, so it is a refinement over model-responsiveness rather than a v1 requirement.

The recommendation is model-responsiveness for v1: it is sound under a weaker assumption than type-directed (the flag participates in the model, not merely that the type exists), it needs only data var-inference already produces, and it degrades to silence rather than to a wrong fact when responsiveness cannot be established. Lineage-directed grounding is the precision path once the var-usage-to-lineage join exists.

Whichever granularity grounds the producing scopes, the check the flag-world analysis exists to perform is unchanged: the flag value flows downstream through the lineage graph, and a downstream contract that pins a specific axis value either agrees with it (pass in this world) or contradicts it (a finding naming the world), which is the "holds under world A, fails under world B" output the design promises.

### Dependency on var-inference

The bridge for value-substitution and control-flow vars depends on var-inference for the var's domain (to enumerate worlds), its type, and its usage locations (to establish responsiveness). The config discoverer does not, which is why config ships first. This dependency is the concrete form of the two-front-end architecture: the bridge is where the Jinja-source view (var identity, domain, usage) meets the compiled-SQL view (scopes, lineage, contracts).

One boundary worth naming, since it shapes the domain the bridge enumerates over: var-inference sources a variable's default from `dbt_project.yml` (and target values from `profiles.yml`), not from an inline `var(name, default)` call site. A variable whose only default is inline is still discovered and typed by name, so it appears in the scaffold, yet it carries no recorded default value. dbt has already folded that inline default into the compiled SQL, so the base world's facts are correct as they stand; what the bridge lacks is a base-world assignment to attach to the flag and a domain member to enumerate from, so such a variable degrades to its single compiled value as one world, the same degrade-not-lie posture the `COMPUTED` case takes. Capturing the inline default, which is present in the parsed AST at the call site, is a natural extension once the enumerator consumes it.

## Soundness contract

The general transfer obligations live in [`propagation-soundness.md`](./propagation-soundness.md) and the facts-specific ones in [`lineage-facts.md`](./lineage-facts.md). The world-specific layer on top:

1. **A world is a closed assumption, and facts bucket by it.** Every `CompileValue` fact carries the one `WorldRef` it was emitted under. Resolution folds only facts sharing a world, so within a world it is the ordinary order-independent meet. A value-substitution var's value is ground truth in its world, the same standing as a native constraint.
2. **Degrade-not-lie at the world level.** An opaque (`COMPUTED`) or open-domain flag becomes one honest world (the value the actual manifest resolved), never a wrong claim and never a crash. Bounded re-compilation that cannot reach a world reports the gap rather than guessing.
3. **A wrong fact is worse than a missing one, so prefer silence on responsiveness.** Model-responsiveness grounds the flag only where the model participates; lineage-directed narrows further. Where responsiveness cannot be established, the flag grounds nothing, and SQL inference and contracts proceed as if no flag fact existed.
4. **Config-derived keys honor the dedup semantics exactly.** A candidate-key fact is emitted only under `merge`-with-key or `delete+insert`, never under `append` or `merge`-without-key, and the adapter default is consulted. The single-batch duplicate hazard under `merge` is surfaced as its own finding, not folded silently into the key claim.
5. **Cross-world disagreement is a finding, not an error.** A contract that holds in some worlds and fails in others produces a per-world finding naming the failing worlds. A genuine contradiction *within* a world (two facts meeting to bottom) is the existing `FactConflictError` path.

## Ergonomics

The deep problem and the ergonomic problem are the same problem seen from two sides: the machinery has to be principled, and the surface has to ask the developer for only what the framework genuinely cannot infer.

- **Minimal authoring.** Config grounding asks the developer for nothing; `unique_key` and `incremental_strategy` are already in their project. Flag grounding asks for the one thing inference cannot supply, the `affects` clause, with type, domain, and usage scaffolded by `dblect scaffold flags` ([`var-inference-spec.md`](./var-inference-spec.md)). The developer writes meaning, not plumbing.
- **Explainability.** A fact that attaches under a world must be traceable to why: which flag, which value, which responsiveness rule, which world. The substrate already exposes a "trace this annotation to its grounding facts" helper; world-indexed facts extend it with the world and the flag. A finding that fires in one world and not another is only actionable if the developer can see the world that breaks it and the chain that put the value there.
- **World output that scales to read.** The per-world report from [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md) ("world: include_tax=False, environment=prod -> FAIL: ...") is right for a handful of worlds. For larger spaces the decision-tree form is the readable one: report the *condition* under which a contract fails (a presence condition over flags) rather than enumerating every passing world, which is the user-facing payoff of the BDD and decision-tree representations.
- **Tractability the developer feels.** DAG factoring and per-contract enumeration (above) keep a contract that depends on two flags from paying for forty, so analysis time tracks the entanglement of the modeling rather than the global flag count. The honest default is to enumerate only the worlds a contract's interaction cluster induces, report the configuration-space coverage achieved, and name the dimensions left un-enumerated.

## Coverage as a first-class output

Silent degradation is sound but can hide behind itself, the same hazard [`lineage-facts.md`](./lineage-facts.md) raises for grounding coverage. The world dimension adds its own version: an analysis that quietly checked one world reads as a clean bill across all worlds. The audit should report the world coverage it achieved, separately from grounding coverage:

- how many worlds a contract was checked under, against the size of its flag-induced space,
- which flags were not enumerated and why (open domain, `COMPUTED`, control-flow not re-compiled),
- and, for the always-present axes, whether both `is_incremental()` states and all reachable `target` worlds were analyzed or only the one the manifest captured.

This is the "no silent caps" rule applied to worlds. The current analyzer's one-world blindness becomes a measured number rather than an unstated assumption, which is itself a reason to land the coverage reporting early.

## What this does not cover

- **The world enumerator.** Choosing worlds, scoping them per contract, and comparing evaluations across them is the flag layer's job. This doc supplies the per-world facts it consumes and the strategy fork it sits on, not the enumerator.
- **The variability-aware Jinja front end.** The variation-preserving parser (choice nodes, fork-merge over Jinja) is the north-star build sketched here, specified elsewhere when it is scheduled.
- **Per-entity flags from seed config tables, external flag platforms, cross-package flag inference, and application-side flags**, all deferred in [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md). The bridge here is the substrate those later discovery adapters feed.
- **Column-level (lineage-directed) responsiveness.** The faithful "produced by responsive code" reading needs the var-usage-to-lineage join across the two front ends; v1 uses model-responsiveness.
- **Activation of conditional facts** from env and target `where`-style filters. The predicate-implication engine ([`predicate.py`](../../src/dblect/lineage/predicate.py)) exists; flowing each scope's filter into it is the same follow-up increment named in [`lineage-facts.md`](./lineage-facts.md).

## Open questions

1. **Config-derived keys: enforced or asserted?** A `merge` key is enforced at write time, which argues for ground-truth standing on the output relation, but the single-batch duplicate hazard means it is not unconditional. Does the candidate-key fact carry an enforcement flag analogous to `NativeConstraint.enforced_on_write`, and does the single-batch hazard ride as a conditional caveat on the same fact or as a wholly separate detector?
2. **Default responsiveness granularity.** Is model-responsiveness the right v1, or is type-directed acceptable as a first cut for structural axes (where SQL inference *can* refute a wrong fact) while model-responsiveness is reserved for user-domain axes (where it cannot)? The asymmetry between refutable and unrefutable axes may justify a per-axis-kind choice.
3. **Bounded re-compilation packaging.** `is_incremental()` and `target` worlds need a second `dbt compile` invocation. Does that live in the manifest-resolution layer (produce N manifests up front) or in the enumerator (request worlds lazily)? The former reuses the existing CLI manifest path; the latter avoids compiling worlds no contract depends on.
4. **When to start the variability-aware front end.** Re-compilation covers correctness; the front end covers scale and removes the live-dbt dependency. What configuration-space size or analysis-time budget is the trigger to invest in it rather than widen re-compilation?
5. **Computing interaction clusters under structure-changing flags.** When a flag changes the DAG itself (`enabled`, a join behind `{% if %}`, a gated `ref`), the influence sets that define clusters are world-dependent. The conservative union over induced structures is sound but can over-cluster. Is the union cheap to compute from var-inference plus the per-world graphs we already build, and is there a tighter sound bound worth the complexity, or is over-clustering an acceptable cost given that it only ever adds worlds?

## References

- The facts substrate this layers on: [`lineage-facts.md`](./lineage-facts.md) and the type surface in [`lineage-facts-types.md`](./lineage-facts-types.md).
- The propagation calculus and its obligations: [`propagation-soundness.md`](./propagation-soundness.md).
- The flag user surface and world enumeration pitch: [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md).
- The Jinja-source front end this depends on: [`var-inference-spec.md`](./var-inference-spec.md).
- The dbt configuration field survey: [`research/dbt-config-patterns.md`](./research/dbt-config-patterns.md).
- Domain-type axis composition: [`domain-type-algebra.md`](./domain-type-algebra.md).

### Literature

- Thomas Thüm, Sven Apel, Christian Kästner, Ina Schaefer, Gunter Saake. "A Classification and Survey of Analysis Strategies for Software Product Lines." ACM Computing Surveys 47(1), Article 6, 2014. The product-based / feature-based / family-based taxonomy.
- Eric Bodden, Társis Tolêdo, Márcio Ribeiro, Claus Brabrand, Paulo Borba, Mira Mezini. "SPLLIFT: Statically Analyzing Software Product Lines in Minutes Instead of Years." PLDI 2013. Lifting IFDS to IDE with BDD-encoded presence conditions, transparent to the base analysis.
- Claus Brabrand, Márcio Ribeiro, Társis Tolêdo, Paulo Borba (TAOSD extension with Johnni Winther). "Intraprocedural Dataflow Analysis for Software Product Lines." AOSD 2012 / TAOSD IX, 2012. The lifted lattice and the spectrum from brute force to aggressive sharing.
- Jan Midtgaard, Aleksandar S. Dimovski, Claus Brabrand, Andrzej Wąsowski. "Systematic Derivation of Correct Variability-Aware Program Analyses." Science of Computer Programming 105, 2015. Variability-aware analyses sound by construction via lifted abstraction.
- Aleksandar S. Dimovski, Claus Brabrand, Andrzej Wąsowski. "Variability Abstractions: Trading Precision for Speed in Family-Based Analyses." ECOOP 2015; "Efficient Family-Based Model Checking via Variability Abstractions," STTT 19(5), 2017. Galois-connection abstractions over the configuration space.
- Aleksandar S. Dimovski, Sven Apel, Axel Legay. "A Decision Tree Lifted Domain for Analyzing Program Families with Numerical Features." FASE 2021. Decision-tree sharing for non-boolean feature spaces.
- Christian Kästner, Paolo G. Giarrusso, Tillmann Rendel, Sebastian Erdweg, Klaus Ostermann, Thorsten Berger. "Variability-Aware Parsing in the Presence of Lexical Macros and Conditional Compilation." OOPSLA 2011 (TypeChef). Choice nodes and presence conditions in one variational AST.
- Paul Gazzillo, Robert Grimm. "SuperC: Parsing All of C by Taming the Preprocessor." PLDI 2012. Fork-merge LR parsing: fork at a conditional, merge on equal parser state.
- Andy Kenner, Christian Kästner, Steffen Haase, Thomas Leich. "TypeChef: Toward Type Checking #ifdef Variability in C." FOSD 2010 workshop (co-located with GPCE/SPLASH). Motivation for checking all configurations at once.
- Martin Erwig, Eric Walkingshaw. "The Choice Calculus: A Representation for Software Variation." ACM TOSEM 21(1), 2011. The formal calculus of variation behind choice nodes. Eric Walkingshaw, Christian Kästner, Martin Erwig, Sven Apel, Eric Bodden. "Variational Data Structures: Exploring Tradeoffs in Computing with Variability." Onward! 2014. Representing and sharing many variants in one structure, the data-structure form of the world-indexed annotation.
- Sahil Thaker, Don Batory, David Kitchin, William Cook. "Safe Composition of Product Lines." GPCE 2007. A property holds in every configuration iff a feature-model-constrained formula is unsatisfiable; the SAT-over-flags view of "which worlds break the contract."
- Ramy Shahin, Marsha Chechik. "Lifting Datalog-Based Analyses to Software Product Lines" (Variability-Aware Datalog). ESEC/FSE 2019. Lift the Datalog engine, not each query; relevant because lineage and taint are Datalog-shaped, so a lifted engine makes them world-aware in one evaluation.
- Todd J. Green, Grigoris Karvounarakis, Val Tannen. "Provenance Semirings." PODS 2007; Yael Amsterdamer, Daniel Deutch, Val Tannen. "Provenance for Aggregate Queries." PODS 2011. The semiring framing dblect's propagator already uses, of which world-indexed annotations are an instance.
- Patrick Cousot, Radhia Cousot. "Abstract Interpretation." POPL 1977; "Systematic Design of Program Analysis Frameworks." POPL 1979. The parameterized-analysis foundation. Thomas Reps, Susan Horwitz, Mooly Sagiv. "Precise Interprocedural Dataflow Analysis via Graph Reachability" (IFDS). POPL 1995; Mooly Sagiv, Thomas Reps, Susan Horwitz. IDE extension, TCS 1996. The distributive-dataflow framework SPLLIFT lifts; the distributivity requirement marks which checks lift cleanly.
- Bruno Blanchet, Patrick Cousot, Radhia Cousot, Laurent Mauborgne, Antoine Miné, et al. "A Static Analyzer for Large Safety-Critical Software" (ASTRÉE), 2003. A parametrizable analyzer adapted to a family of related programs.
</content>
