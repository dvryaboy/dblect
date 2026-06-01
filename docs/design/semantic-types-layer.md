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

The default is `preserve`: a value-returning scalar (alias, rename, a function that returns the same column) keeps the type's axes. The exceptions are declared at the SQL site, not on the type, using the closed annotation vocabulary from the digest (`dblect: preserves`, `discount(N)`, `tax(rate)`, `currency(from, to)`), because whether `revenue * 0.9` is a discount or a currency conversion is a property of that line of SQL, not of the type. An unannotated opaque scalar clears the axes and asks for an annotation. A type declares no scalar behavior of its own in v1; the SQL site is the only place a transform is annotated (decision 2 in the appendix).

### Behavior under aggregation (combinability, v1)

Two orthogonal knobs, both with safe defaults, covering the v1 scope agreed for the substrate (coherence plus a summability flag; per-dimension semi-additivity deferred):

- **Coherence** (`within=<cols>`): the named columns must be constant across the aggregated rows or the result is a category error. This applies to *every* cross-row aggregate, additive or value-selecting: summing, averaging, or taking the max across mixed currencies are all meaningless. Default: no coherence requirement. This is the declaration that compiles to a `depends_on` edge on the functional-dependency property, since "constant within the group" is an FD the aggregate transfer reads.
- **Summability** (`summable: bool`): whether additive aggregates (`SUM`, `AVG`) preserve the meaning. A ratio or a percentage is not summable; summing it is a bug. Value-selecting aggregates (`MIN`, `MAX`, `FIRST`, `LAST`) still preserve a non-summable type, since they return an existing value, and `COUNT` produces a different type regardless. Default: `summable = True`.

These are orthogonal: money is summable but coherence-bound (`summable=True, within="currency"`); a conversion rate is coherence-free but not summable (`summable=False`). The pair maps onto the substrate's three aggregate-transfer outcomes: preserved, preserved-under-coherence, or cleared.

### Constraints versus axes

`Field(non_negative=True)` and `Field(contains_tax=False)` look alike but are different in kind. `non_negative` is a checkable value constraint: it grounds a fact the runtime layer can verify against data, closer to the `PROVEN` posture. `contains_tax` is a `VOUCHED` meaning the framework propagates and trusts. The surface is one (`Field`), the trust class is two. The doc should keep that distinction visible, because it decides which findings are unconditional.

## Multi-column concepts

Per the intro doc's rule, money, ranges, and addresses are modeled as separate columns linked by declarations, not as record-shaped types. Coherence is the linking mechanism: `within=<sibling column>` ties a measure to the column that must stay constant for it to aggregate. The split is that the *type* carries that it is coherence-bound, and the `Field` at the binding names the column, since the column exists only at the model (`amount: Money = Field(within="currency")`; decision 1 in the appendix). `Money` is then thin sugar, a hand-written `SemanticType` that is a `Decimal` measure coherence-bound to its denomination column, and there is no currency-specific concept anywhere in the framework. Unit-of-measure, fiscal entity, and scenario reuse the same `within`.

## How declarations reach the substrate

A type binding compiles to substrate inputs, and the user sees none of them:

- The type compiles to a single `Property[K, ColumnRef]`: a value-domain axis lattice, the scalar and aggregate transfers below, and `semiring=None` (a user-domain axis does not count or accumulate, so it carries no operator-algebra slot). The compiled property joins the run through the substrate's `PropertyRegistry`, which assigns its evaluation order from `depends_on` and rejects a name collision with a built-in. Registration is the seam: a developer-defined type is one more registry entry, indistinguishable from nullability once compiled.
- Each refinement axis on a bound column becomes a `USER_ASSERTED` `Fact[K]` at that column's scope.
- The scalar and aggregate behaviors become the property's operator and aggregate transfers.
- A coherence declaration becomes a `depends_on` edge on the functional-dependency property plus the aggregate transfer that reads it. The edge is the depended-on property's `ref`, so the substrate orders the FD property before this one; the read flows through a typed `DepContext` rather than a global.
- The type's display name and optional one-line description fill the substrate's `display` slot (`AxisDisplay`), which the seam diagnostic reads when this axis is the one that clears at a typed/untyped boundary. The framework authors none of that text; it plumbs the name the modeler chose, falling back to the bare type and axis names when the type supplies no description.
- Boundary checking (does a producer column satisfy a consumer's declared type) is the substrate's `consistent` check over the user-domain precision order, the same machinery nullability uses.

The user writes `RevenueNet`, `Field(within="currency")`, `summable=False`. They never see `Property`, `PropertyRegistry`, `Scope`, `Fact`, `depends_on`, `AxisDisplay`, or a transfer. That round-trip is the test that the substrate can carry the surface.

## Decisions

The calls made for v1, with the reasoning and what each gives up recorded in the appendix.

1. Coherence is split: kind on the type, which-column on the `Field` binding.
2. Scalar transforms are annotated at the SQL site only; a type declares no scalar behavior.
3. The constraint-versus-axis distinction stays internal; the report labels findings by trust class.
4. Summability is a single boolean for v1.
5. Currency-as-axis versus currency-as-column follows the existing global-versus-per-row rule of thumb.
6. Boundary subtyping reuses the substrate `consistent` check (a producer type must refine the consumer's expectation).

## What this does not cover

- The declaration *syntax* and the declaration-registry mechanics where Python `ModelContract`s are collected ([`dblect_technical_intro.md`](./dblect_technical_intro.md)); distinct from the substrate's `PropertyRegistry`, which orders compiled properties.
- The propagation engine and fact substrate ([`lineage-facts.md`](./lineage-facts.md)).
- Flags and world enumeration ([`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md)); a flag's `affects` is another producer of facts under a chosen world.
- Opaque scalar and UDF handling ([`udf-and-opaque-operators.md`](./udf-and-opaque-operators.md)).
- Per-dimension semi-additivity, deferred past v1.

## Appendix: decisions and tradeoffs

Each entry records the choice, the alternative weighed, what the choice buys, what it gives up, and what would make us revisit.

### 1. Coherence split: kind on the type, column on the binding

- **Choice.** A `SemanticType` declares that it is coherence-bound (Money is bound to its denomination); the `Field` at the `ModelContract` names the actual column (`amount: Money = Field(within="currency")`).
- **Alternative.** Put the column name on the type definition.
- **Buys.** Types stay reusable across models, since a model-specific column name never gets baked into a shared type.
- **Gives up.** The type alone does not fully specify behavior; a `Money` used without a `within` binding has no coherence and aggregates as a plain decimal, which a careless binding could miss.
- **Revisit.** If a type's coherence column is almost always the same name, add an optional type-level default that the binding can override.

### 2. Scalar transforms at the SQL site only

- **Choice.** Types declare no scalar behavior. The default is `preserve`; transforms are annotated on the SQL line (`dblect: discount(N)`, `tax(rate)`, `currency(from, to)`); an unannotated opaque scalar clears.
- **Alternative.** Let a type carry default scalar rules (for example, "any arithmetic clears the currency axis").
- **Buys.** A smaller type surface, and the annotation lives where the decision is actually made, since whether `revenue * 0.9` is a discount or a conversion is a property of that line, not the type.
- **Gives up.** A type with a genuinely uniform scalar rule must repeat the annotation at every site; there is no write-once type-level rule.
- **Revisit.** If repetition becomes painful for a common type, add an optional type-level scalar default.

### 3. Constraint-versus-axis distinction kept internal

- **Choice.** One `Field` surface. The framework classifies checkable constraints (`non_negative`, `PROVEN`-like, runtime-verifiable) apart from `VOUCHED` axis values internally, and the report labels findings by trust class.
- **Alternative.** Make the user mark which is which.
- **Buys.** The user writes `Field(...)` without learning the trust taxonomy, while the report still communicates which findings are unconditional.
- **Gives up.** The user cannot override the classification, and the framework must classify every `Field` key reliably (a key is either a known constraint primitive or an axis).
- **Revisit.** If a `Field` key is genuinely ambiguous between the two, add an explicit marker for that case.

### 4. Summability as a single boolean

- **Choice.** `summable: bool` (default `True`), orthogonal to `within`. Value-selecting aggregates (`MIN`, `MAX`, `FIRST`, `LAST`) preserve even when `summable=False`; `COUNT` changes the type regardless.
- **Alternative.** A richer per-aggregate or per-dimension model.
- **Buys.** A minimal surface that already covers the common cases: currency coherence and non-summable ratios.
- **Gives up.** Cannot express semi-additive-over-a-dimension (a balance summable across accounts but not across time).
- **Revisit.** When a real semi-additive measure appears; this is the deferred per-dimension model.

### 5. Currency-as-axis versus currency-as-column

- **Choice.** Follows the existing global-versus-per-row rule of thumb. Currency fixed for a whole column is an axis (`Revenue.refine(currency="USD")`, static); currency varying per row is a column plus coherence (`within="currency"`).
- **Why no new mechanism.** Both arms already exist; the type author picks on whether the value varies per row. Recorded here only because the two surfaces look similar.

### 6. Boundary subtyping reuses `consistent`

- **Choice.** A producer column satisfies a consumer's declared type when the producer type *refines* the consumer's expectation (`RevenueNet` is accepted where `Revenue` is expected; `Revenue` is a finding where `RevenueNet` is required). This is the substrate `consistent` check over the user-domain precision order, standard subtyping.
- **Why no new mechanism.** The substrate already computes this for every property; the types layer adds nothing.

## References

- [`dblect_technical_intro.md`](./dblect_technical_intro.md): the declaration surface this layer's semantics attach to.
- [`lineage-facts.md`](./lineage-facts.md): the substrate these declarations compile to (facts, transfers, `depends_on`, `consistent`, the `PropertyRegistry` a compiled property registers through, and the `AxisDisplay` slot the seam diagnostic reads).
- [`design-concepts-digest.md`](./design-concepts-digest.md): the two trust classes and the Pandera-shaped-surface constraint.
- Pandera and Pydantic for the declaration pattern; the type-qualifier tradition (CQual, FlowCaml) for refinement axes as a user-domain lattice; Kimball measure additivity for the summability distinction.
