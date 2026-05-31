# Semantic types: what a type declares for propagation

Status: design notes (skeleton). Sketches the bridge between the declaration surface and the propagation substrate, and collects the open questions to think through. Sections marked *Sketch* are deliberately thin.
Audience: engineers working on the types layer, and anyone who has read [`dblect_technical_intro.md`](./dblect_technical_intro.md) (the declaration surface) and [`lineage-facts.md`](./lineage-facts.md) (the substrate) and wants to know how the two meet.

## Where this fits

[`dblect_technical_intro.md`](./dblect_technical_intro.md) covers how a user *writes* semantic types and contracts: classes with annotated fields, `Field(...)`, `.refine(...)`, `ModelContract`, column proxies. [`lineage-facts.md`](./lineage-facts.md) covers how the engine *propagates and checks* a `Property[K]`. This doc is the layer in between: the small set of things a semantic type must declare so the substrate can carry it, and the discipline that keeps those declarations Pandera-shaped rather than turning into propagation rules.

The governing constraint, from [`design-concepts-digest.md`](./design-concepts-digest.md): a user declares meaning, never a transfer function. Anywhere this doc would have a user write something that looks like an operator rule, that is a smell to design out.

## What a semantic type already declares

Recap from the intro doc, not re-derived here. A `SemanticType` is scalar (wraps one column) and declares a base SQL type plus refinement axes whose values are Python primitives or enums. `.refine(...)` derives a more specific variant. `Field(...)` attaches per-column refinements and constraints at a `ModelContract` binding. A semantic type's refinement axes are the values of a user-domain `Property[K]`, which is `VOUCHED`: its soundness is conditional on the declarations being accurate.

## What a type declares for propagation

Beyond the axes themselves, the substrate needs to know how a type behaves as it flows through SQL. The aim is that almost all of this is a default, and the user declares only the exceptions.

### Behavior at scalar operators

The default is `preserve`: a value-returning scalar (alias, rename, a function that returns the same column) keeps the type's axes. The exceptions are declared at the SQL site, not on the type, using the closed annotation vocabulary from the digest (`dblect: preserves`, `discount(N)`, `tax(rate)`, `currency(from, to)`), because whether `revenue * 0.9` is a discount or a currency conversion is a property of that line of SQL, not of the type. An unannotated opaque scalar clears the axes and asks for an annotation. So the type itself usually declares nothing here.

*Sketch:* whether a type ever wants to declare a default scalar behavior (for example, "any arithmetic clears the currency axis") or whether SQL-site annotation is always the right place is an open question below.

### Behavior under aggregation (combinability, v1)

Two orthogonal knobs, both with safe defaults, covering the v1 scope agreed for the substrate (coherence plus a summability flag; per-dimension semi-additivity deferred):

- **Coherence** (`within=<cols>`): the named columns must be constant across the aggregated rows or the result is a category error. This applies to *every* cross-row aggregate, additive or value-selecting: summing, averaging, or taking the max across mixed currencies are all meaningless. Default: no coherence requirement. This is the declaration that compiles to a `depends_on` edge on the functional-dependency property, since "constant within the group" is an FD the aggregate transfer reads.
- **Summability** (`summable: bool`): whether additive aggregates (`SUM`, `AVG`) preserve the meaning. A ratio or a percentage is not summable; summing it is a bug. Value-selecting aggregates (`MIN`, `MAX`, `FIRST`, `LAST`) still preserve a non-summable type, since they return an existing value, and `COUNT` produces a different type regardless. Default: `summable = True`.

These are orthogonal: money is summable but coherence-bound (`summable=True, within="currency"`); a conversion rate is coherence-free but not summable (`summable=False`). The pair maps onto the substrate's three aggregate-transfer outcomes: preserved, preserved-under-coherence, or cleared.

### Constraints versus axes

`Field(non_negative=True)` and `Field(contains_tax=False)` look alike but are different in kind. `non_negative` is a checkable value constraint: it grounds a fact the runtime layer can verify against data, closer to the `PROVEN` posture. `contains_tax` is a `VOUCHED` meaning the framework propagates and trusts. The surface is one (`Field`), the trust class is two. The doc should keep that distinction visible, because it decides which findings are unconditional.

## Multi-column concepts

Per the intro doc's rule, money, ranges, and addresses are modeled as separate columns linked by declarations, not as record-shaped types. Coherence is the linking mechanism: `within=<sibling column>` ties a measure to the column that must stay constant for it to aggregate. `Money` is then thin sugar, a hand-written `SemanticType` that is a `Decimal` measure with coherence on its denomination column, and there is no currency-specific concept anywhere in the framework. Unit-of-measure, fiscal entity, and scenario reuse the same `within`.

## How declarations reach the substrate

A type binding compiles to substrate inputs, and the user sees none of them:

- Each refinement axis on a bound column becomes a `USER_ASSERTED` `Fact[K]` at that column's scope.
- The scalar and aggregate behaviors become the property's operator and aggregate transfers.
- A coherence declaration becomes a `depends_on` edge on the functional-dependency property plus the aggregate transfer that reads it.
- Boundary checking (does a producer column satisfy a consumer's declared type) is the substrate's `consistent` check over the user-domain precision order, the same machinery nullability uses.

The user writes `RevenueNet`, `Field(within="currency")`, `summable=False`. They never see `Scope`, `Fact`, `depends_on`, or a transfer. That round-trip is the test that the substrate can carry the surface.

## Open questions

The parts genuinely unsettled, which is the point of this sketch.

1. **Where coherence is declared.** `within="currency"` names a sibling column, which only exists at a `ModelContract` binding, not in a reusable `SemanticType` definition. So does the column reference live on the `Field` at the binding (`amount: Money = Field(within="currency")`), with the type carrying only "Money is coherence-bound to its denomination"? That split (kind-of-coherence on the type, which-column on the binding) seems right but needs confirming.
2. **Granularity of the non-summable case.** `summable=False` clears under `SUM`/`AVG` but value-selecting aggregates still preserve. Is a single boolean enough for v1, or do real measures need to distinguish "no additive aggregate" from "no aggregate at all"? Leaning boolean for v1.
3. **Type-level versus SQL-site declaration for scalar behavior.** The digest puts transform annotations at the SQL site. Does a type ever need a default scalar behavior of its own, or is SQL-site annotation always correct? If types never declare scalar behavior, the surface stays smaller.
4. **One `Field`, two trust classes.** Constraints (checkable, `PROVEN`-like) and axes (`VOUCHED`) share the `Field` surface. Do we surface the distinction to the user at all, or keep it internal and let the report label findings by trust class?
5. **Refinement-axis enums and coherence columns.** The stdlib ships `Currency`, `Country` as enums (axis values), and also has currency-as-a-column (coherence). How do a `Currency`-typed *column* and a `currency` *axis value* relate, and when does a user reach for which?
6. **Subtyping at boundaries.** The precise rule for when a producer's refined type satisfies a consumer's declared type (is `RevenueNet` acceptable where `Revenue` is expected, and vice versa), expressed as the user-domain precision order the `consistent` check uses.

## What this does not cover

- The declaration *syntax* and registry mechanics ([`dblect_technical_intro.md`](./dblect_technical_intro.md)).
- The propagation engine and fact substrate ([`lineage-facts.md`](./lineage-facts.md)).
- Flags and world enumeration ([`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md)); a flag's `affects` is another producer of facts under a chosen world.
- Opaque scalar and UDF handling ([`udf-and-opaque-operators.md`](./udf-and-opaque-operators.md)).
- Per-dimension semi-additivity, deferred past v1.

## References

- [`dblect_technical_intro.md`](./dblect_technical_intro.md): the declaration surface this layer's semantics attach to.
- [`lineage-facts.md`](./lineage-facts.md): the substrate these declarations compile to (facts, transfers, `depends_on`, `consistent`).
- [`design-concepts-digest.md`](./design-concepts-digest.md): the two trust classes and the Pandera-shaped-surface constraint.
- Pandera and Pydantic for the declaration pattern; the type-qualifier tradition (CQual, FlowCaml) for refinement axes as a user-domain lattice; Kimball measure additivity for the summability distinction.
