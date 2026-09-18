# UDFs and opaque operators

Status: design notes (skeleton). Captures the direction and the prior-art map so there is a placeholder to grow. Sections marked *Sketch* are deliberately thin and need a fuller pass before implementation.
Audience: engineers working on property propagation (`column-level-lineage.md`) or on the generator (`contract-directed-generation.md`) who hit a scalar function, UDF, or expression the framework cannot see through.

## Why this is its own doc

Property propagation and contract verification both stumble on the same obstacle: an operator the framework cannot reason through. A warehouse-native function, a user-defined function, or an arithmetic expression with a bare literal is *opaque* in the sense that the framework does not know how it transforms either a refinement value (the static side) or the data (the generation side).

This is orthogonal to [`lineage-facts.md`](./lineage-facts.md). Facts ground *leaf values* from declarations. This doc is about *transfer rules through operators*, which is a propagation-and-generation concern. It intersects [`column-level-lineage.md`](./column-level-lineage.md) (the per-operator transfer slot) and [`contract-directed-generation.md`](./contract-directed-generation.md) (the intent catalog and fill layer). It does not touch fact grounding or refinement-axis design.

The current posture across the design is conservative and correct as far as it goes: literals erase refinement and ask for an annotation ([`design-concepts-digest.md`](./design-concepts-digest.md), "Literals are opaque to refinement propagation"), window regions erase and ask for re-annotation, and `@dblect.function` is the escape hatch for project-specific operators. This doc asks whether the field gives us a more graduated treatment than a single erase-or-annotate rule, and where the cheap wins are.

## A transparency gradient, not a binary

"Opaque" is not one thing. The treatment should follow how much of the operator the framework can actually inspect.

- **Inlinable.** A SQL UDF or a dbt macro whose body is available and parses into our AST. There is nothing opaque about it: inline the body and propagate through it with the operator rules we already have. The cheapest win, and it subsumes much of what one would otherwise reach for heavier machinery to do.
- **Declared-signature.** A function whose body is unavailable or foreign (a Python or JavaScript UDF, a warehouse builtin), but whose effect the user can state. This is the existing annotation surface generalised: `dblect: preserves` / `discount(N)` / `tax(rate)` / `currency(from, to)`, the `SemanticFlag.affects` pattern, and `@dblect.function`. The framework trusts the declared signature the way it trusts any `VOUCHED` input.
- **Fully opaque.** No body, no declared signature (an undocumented builtin, an external UDF nobody has annotated). Erase to the property default and surface a warning at the consumer, so the loss of information is visible rather than silent.

*Sketch:* the framework should detect which tier a given function falls into from the manifest and catalog (is the body present? is it parseable by sqlglot? is there a declared signature?), and pick the treatment automatically, reporting the tier so a reviewer sees why a column went opaque.

## Static treatment per tier

*Sketch.*

- Inlinable: substitute the body, propagate, no information lost. Needs care with argument binding and with recursion or warehouse-specific constructs the body may contain, which fall back to the lower tiers.
- Declared-signature: apply the declared transfer (`preserve` / `erase` / a per-axis effect). This is where the composition-rule vocabulary from the digest lands for opaque operators.
- Fully opaque: erase to the property default; the validation layer then treats a declaration at that node as anchoring an opaque upstream (the vacuous-pass row in `lineage-facts.md`).

The structural properties (`PROVEN`) and the user-domain properties (`VOUCHED`) differ here. A structural property can often stay precise through an opaque scalar (a row-preserving scalar function does not change cardinality or candidate keys, whatever it computes), while a user-domain property usually cannot (whether `f(revenue)` preserves tax inclusion depends on what `f` does). So the tier interacts with the property's trust class, and an opaque scalar is frequently `erase` for meaning while `preserve` for structure.

## Generation treatment per tier

*Sketch.* The generator's problem is the dual one: produce fixture data that flows *through* an opaque operator so a downstream contract is actually exercised, and ideally that covers the operator's behaviour rather than hitting one arbitrary path.

- Flow-through: get non-empty, illustrative data past selective opaque operators (filters, joins, UDFs that gate rows), so the downstream contract sees rows. This is the ILLUSTRATE problem (below).
- Coverage: when the body is available, generate inputs that exercise the operator's branches, so the contract is tested against the operator's behaviour rather than one sample. This is the BigTest and Qex problem (below).

Both connect to the existing intent catalog: the intents fix structural shape on the contract-relevant columns, and this is the residual question of what to put through an opaque transform so the shaped data survives to the contract.

## Prior art

The UDF-and-dataflow-testing literature has two complementary stances, and dblect wants both for different halves.

- **Black-box, flow-through (ILLUSTRATE).** Olston, Chopra, Srivastava, *Generating Example Data for Dataflow Programs* (SIGMOD 2009), building on Pig Latin (Olston et al., SIGMOD 2008). Generates small, illustrative example data that flows through a whole dataflow program and yields non-empty, meaningful output at each operator, treating UDFs as black boxes it probes by running, and combining downstream-to-upstream constraint propagation with synthesis and sampling from real data. Two ideas transfer directly: getting fixture rows through an opaque transform so a downstream contract is exercised, and the conciseness objective, which is dblect's minimal-counterexample goal under another name.
- **White-box, coverage (BigTest).** Gulzar, Mardani, Musuvathi, Kim, *White-Box Testing of Big Data Analytics with Complex User-Defined Functions* (ESEC/FSE 2019). Symbolically executes the UDF body together with dataflow operator semantics and uses an SMT solver to generate minimal inputs achieving logical coverage of the combined program. The pointer for dblect is "look inside when you can," which in the SQL world mostly reduces to inlining a parseable UDF body rather than running symbolic execution, with symbolic methods reserved for foreign-language bodies.
- **SQL-native input generation.** Qex (Veanes et al., Microsoft Research) generates input tables satisfying a SQL query's conditions via SMT, and EvoSQL (Castelein, Aniche, et al.) does search-based generation of rows covering SQL predicate branches. These are the SQL cousins of BigTest's coverage idea and the natural reference for generating data through SQL UDFs and selective predicates.

dblect's own posture (literals opaque, erase-and-annotate, window regions as erase boundaries) sits alongside these as the conservative default the graduated treatment above would refine.

*Citations to verify before this leaves draft: exact venues and years, particularly Qex and EvoSQL.*

## Open questions

*Sketch.*

- How far to push inlining. dbt macros and SQL UDFs are parseable; how much of the long tail (recursion, dialect-specific bodies, dispatch) is worth following versus dropping to the declared-signature tier.
- Whether foreign-language UDF bodies (Python, JavaScript) ever justify symbolic execution in dblect, or whether the declared-signature tier plus runtime probing is always the better cost tradeoff.
- How the generator decides when flow-through sampling is enough and when coverage of an opaque operator's branches is worth the cost.
- Whether the opaque-tier classification is surfaced as a first-class finding ("this column went opaque at `f(...)`; declare a signature to keep analysis") the way the unenforced-constraint case is in `lineage-facts.md`.

## What this does not cover

- Fact grounding and refinement-axis design ([`lineage-facts.md`](./lineage-facts.md), the types layer).
- Window-function propagation, which has its own scope decision in [`design-concepts-digest.md`](./design-concepts-digest.md) and overlaps here only in that a window region is one kind of erase boundary.
- The general generator architecture; this doc is only the opaque-operator slice of it.

## References

- [`column-level-lineage.md`](./column-level-lineage.md): the per-operator transfer slot this refines.
- [`contract-directed-generation.md`](./contract-directed-generation.md): the intent catalog and fill layer the generation treatment plugs into.
- [`design-concepts-digest.md`](./design-concepts-digest.md): the existing opaque-literal and window-erase decisions.
- Olston, Chopra, Srivastava, *Generating Example Data for Dataflow Programs* (SIGMOD 2009); Olston et al., *Pig Latin* (SIGMOD 2008).
- Gulzar, Mardani, Musuvathi, Kim, *White-Box Testing of Big Data Analytics with Complex User-Defined Functions* (ESEC/FSE 2019).
- Veanes et al., Qex (symbolic SQL query exploration); Castelein, Aniche, et al., EvoSQL (search-based SQL test-data generation).
