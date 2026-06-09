# dblect DSL reference

A dense, single-page reference for the declaration DSL: what you can write, on what
object it lives, and what each construct means. The narrative and the "why" live in
the companion docs, cited per section. This page is a forward-looking surface drawn
from those designs, so some entries are settled and some are still being specified;
where a spelling is open, it says so.

Conventions used below: `T` is a `DomainType`, `C` a `ModelContract`, `col` a column
proxy. "Magnitude" means a summable field (backed by `Decimal`, `Count`, and friends);
"tag" means a field that must agree when values combine (backed by an enum, `bool`, or
`Currency`). The magnitude/tag split is inferred from the field's type, never annotated.

See also: [declaration-dsl.md](declaration-dsl.md), [domain-type-algebra.md](domain-type-algebra.md),
[dblect_technical_intro.md](dblect_technical_intro.md), [flags_and_configs_as_types.md](flags_and_configs_as_types.md),
[domain-type-functions.md](domain-type-functions.md).

---

## `DomainType`

A composite type that carries domain meaning. Each annotated field is a facet.
See [declaration-dsl.md](declaration-dsl.md) (types, refinement, extension).

- `class Money(DomainType)` with `amount: Decimal(18, 2)`, `currency: Currency`.
  Declares the type and its fields. A field is **physical** (its value varies per row,
  stored in the warehouse) or **logical** (one value for the whole column in a build,
  fixed by the type or a flag). Which one a field is depends on how the type is *used*,
  not how it is defined.
- `T.refine(**field=value) -> T'`. Returns a subtype with those fields narrowed to fixed
  values. Partial and chainable (`RevenueNet.refine(currency=Currency.USD)`). Use it to
  name a reusable type: `RevenueNet = Revenue.refine(contains_tax=False, contains_discount=True)`.
- `T.columns(**field="col") -> T'`. Binds each physical field to a warehouse column by
  name. This is the explicit, all-physical binding form, and the only one that can point
  several amount fields at one shared currency column.
- `T(**field=value_or_col)` (call form). At a binding site, fixes a field to a value
  (`Money(currency=Currency.USD)`) and/or maps a field to a differently-named column
  (`Money(amount="order_total")`) in one call. Reads as inline `refine` plus `columns`.
- `class Taxed(Revenue)` with `contains_tax: bool = True`. Subclassing both extends (adds
  new fields) and fixes inherited ones (the class-level twin of `.refine()`).
- `class TaxedShipped(Taxed, Shipped)`. Multiple inheritance unions facets. Where two
  parents fix the same field the values must agree; otherwise the result carries both.

Binding rule: a field binds to the warehouse column of the **same name**, and nothing
else is inferred. When the column is named differently, map it (`T(amount="col")` or
`T.columns(...)`). Open spelling: whether `T(...)` is exactly `T.refine(...)` and how far
a name-matching shorthand should reach are noted as open questions in
[declaration-dsl.md](declaration-dsl.md).

---

## `ModelContract`

Binds domain types to a dbt model's columns and, optionally, declares invariants over
them. A contract with only column declarations and no methods is valid and already buys
type propagation. See [declaration-dsl.md](declaration-dsl.md) (ModelContract).

- `class FctOrders(ModelContract)`. Declares the contract.
- `dbt_model = "marts.fct_orders"`. The manifest reference, resolved with dbt's `{{ ref() }}`
  rules. A misspelled model or renamed column surfaces as a finding, not an import crash.
- `col: T`. Binds column `col` to type `T` by exact field-name match; map explicitly when
  names differ. Scalar marker types (below) type their column directly.
- `col: T = dblect.Field(...)`. Attaches column-level metadata. Constraint kwargs follow
  Pydantic (`ge`, `gt`, `le`, `lt`, `multiple_of`, `min_length`, `max_length`), with
  readable aliases (`non_negative=True`), and the same call can fix domain fields inline
  (`Field(contains_tax=False)`).
- `requires_flags = {...}`. Restricts the contract to the flag worlds named (see Flags).
  Authoritative form in [flags_and_configs_as_types.md](flags_and_configs_as_types.md).

### Built-in field markers and types

Used directly as a column's type.

- `PrimaryKey`. The column, or a tuple of columns, is unique. The column-scoped spelling of
  a `key` fact (see Contracts). Also read from a dbt `unique` test.
- `ForeignKey("model.column")`. References another model's column. The column-scoped
  spelling of a `foreign_key` fact, also read from a dbt `relationships` test, and used as
  an edge for fixture generation.
- `Count`. A count magnitude, always safe to sum.
- `Money`. `amount: Decimal` together with `currency: Currency`.
- `Currency`, `Country`. Enum tags (ISO 4217, ISO 3166).
- `Decimal(precision, scale)`, `Date`, `Timestamp`, `Varchar`, `Integer`, `BigInt`,
  `Boolean`, `Uuid`, `Json`. Base SQL types with convenience constructors.

---

## Contracts (`@contract`)

A contract is a method marked `@contract` whose body returns a symbolic expression over
column proxies. What the contract *does* is decided by the type of value it returns, which
is also the line between what the analyzer reasons with and what it only runs.
See [dblect_technical_intro.md](dblect_technical_intro.md), [propagation-soundness.md](propagation-soundness.md).

**Return a fact, and the analyzer reads it.** A fact is trusted at analysis time to
discharge obligations and propagate, and verified against data, so a violation becomes its
own finding. Facts are a closed vocabulary, exactly the structural properties the substrate
propagates ([lineage-facts.md](lineage-facts.md)), built from column proxies.

- `self.key(self.a, self.b)`. The columns are unique together.
- `self.col.references(models.dim.key)`. A referencing edge into another relation.
- `self.a.determines(self.b)`. The functional dependency `a -> b`; lets a grouped sum keep
  a per-row tag.
- `self.grain(per=self.order_id)`. This relation has one row per key.

Every fact is a method on its natural subject (a column, or the relation `self`), the same
shape as the rest of the proxy API. The single-column facts have field-marker sugar
(`order_id: PrimaryKey`, `customer_id: ForeignKey("dim.key")`) that desugars to `key` and
`references`.

**Return a predicate, and the analyzer only verifies it.** An equality or boolean over
aggregates is run but never reasoned with. Tolerance lives in the expression, not in a
decorator argument: `(a.sum() == b.sum()).within(0.01)`, optionally `.relative_to(ref)`. A
predicate written over materialized frames rather than proxies is the escape hatch,
verify-only by construction because the analyzer cannot read it symbolically.

Join cardinality is not asserted. It follows from grain, keys, and foreign keys (the
cardinality-reads-uniqueness path in [propagation-soundness.md](propagation-soundness.md)),
so you state the grain and let the rest follow.

### Scoping

Modifiers narrow where a contract applies and compose with any contract above.

- `@contract(when=predicate)`. The contract runs only where a structural or runtime
  precondition holds (`@contract(when=models.fct_returns.row_count() > 0)`).
- `self.where(predicate)`. Restricts to matching rows.
- `requires_flags = {...}`. Restricts to named flag worlds.
- `class G(ContractGroup)` with `when = predicate`. Shares one precondition across several
  contracts, written once.

Deferred: replay-determinism and late-arrival contracts reach into temporal consistency,
the least-developed corner of this surface and not being built yet. They are left for a
dedicated pass and not specified here (see the note in
[dblect_technical_intro.md](dblect_technical_intro.md)).

---

## Column proxies

The expression objects you manipulate inside a contract body. `self.col` references a
column on the contract's own model; `models.stg_orders.subtotal` references one on
another model. Proxies are symbolic: they build an expression and fetch no data. Operators
are overloaded, so the result of a comparison is a predicate, not a Python bool.
See [dblect_technical_intro.md](dblect_technical_intro.md) and [domain-type-algebra.md](domain-type-algebra.md).

- Aggregations: `col.sum()`, `col.avg()`, `col.min()`, `col.max()`, `col.count()`,
  `col.count_distinct()`. Reduce a column to one value. A reduction over one field of a
  multi-field type is well-typed only when the type's other fields are constant across the
  group (see `determines` and `group_by`); `count` is always safe.
- `agg.group_by(*cols)`. Makes the aggregation per-group. The grouping keys are what the
  framework checks a companion tag against.
- `self.a.determines(self.b)`. Builds the functional-dependency fact `a -> b`. The fact
  vocabulary (`determines`, `references`, `key`, `grain`) is defined under Contracts.
- Arithmetic `+`, `-`, `*`, `/`. Builds value expressions. Magnitudes combine by the
  dimensional algebra (`*` adds unit exponents, `/` subtracts); tags must agree, and a
  scalar multiply rides a nominal tag through unchanged.
- Comparison `==`, `!=`, `<`, `<=`, `>`, `>=`. Builds predicates; the operands' tags must
  agree.
- Boolean `&`, `|`, `~`. Combines predicates (Polars convention, not Python `and`/`or`).
- Row predicates: `col.is_null()`, `col.is_not_null()`, `col.in_(values)`,
  `col.between(lo, hi)`, `col.equals(value)`.
- Tolerance: `lhs.within(eps)` sets an absolute tolerance for an equality;
  `.relative_to(ref)` reinterprets it as relative.
- Scoping: `self.where(predicate)` restricts a contract to matching rows.
- Model-level: `models.M.exists()`, `models.M.row_count()`, evaluated at analysis time and
  handy as `@contract(when=...)` preconditions.

---

## Flags and worlds (`DomainFlag`)

Types a dbt var or env var whose value changes what columns mean, so the framework can
enumerate the worlds it induces and check each. See
[flags_and_configs_as_types.md](flags_and_configs_as_types.md) and
[var-inference-spec.md](var-inference-spec.md).

- `class IncludeTaxInRevenue(DomainFlag)`. Declares the flag. Body fields: `dbt_var`
  (or `env_var`), `type` (`bool`, `int`, `str`, or an enum), `domain` (the values to
  explore), `default`, and `affects`.
- `affects = RefinementEffect(target=..., value_when_true=..., value_when_false=...)`, or
  `RefinementEffect(target=..., value_map={...})` for an enum flag. Maps each flag value to
  a field fix on a type.
- `CompositeEffect(...)`, `ConditionalEffect(...)`, `OpaqueEffect()`. Combine several
  effects, condition an effect, or declare the flag affects meaning in a way dblect does
  not model (columns lose their refinement under it).
- `Flag.is_true()` and similar read as predicates for `@contract(when=...)` and `requires_flags`.

The framework explores the product of all flag domains, propagates types in each world,
and reports per-world findings. Exact constructor shapes are the record of
[flags_and_configs_as_types.md](flags_and_configs_as_types.md).

---

## Function signatures (`@dblect.signature`)

Declares how a SQL function transforms dimensional types, so unit-carrying values stay
sound across user-defined operations. See [domain-type-functions.md](domain-type-functions.md).

- `@dblect.signature("convert_currency")` on `def _(amount: Money[U], rate: Rate[V, U]) -> Money[V]: ...`.
  Unit variables (`U`, `V`) tie the result's dimension to the arguments'. Result
  dimensions can be monomials (`Quantity[U ** 2]`, `Quantity[U * V]`) or `Dimensionless`
  (required for transcendentals such as `exp`, `log`).

---

## Inline SQL annotations

For scalar expressions whose effect on refinements cannot be inferred from the SQL alone.
See [design-concepts-digest.md](design-concepts-digest.md).

- `-- dblect: preserves`. The expression keeps its operand's refinements.
- `-- dblect: discount(0.10)`, `-- dblect: currency(from, to)`. Names a known transform on
  the refinement (a discount applied, a currency converted).
- A fixture opt-out marker excludes an opaque region from generation rather than guessing
  inputs for it.

---

## Notes on spellings still in motion

- `T(...)` call form versus `T.refine(...)`: treated as equivalent here; whether the call
  form is exactly that sugar or reserved for inline use is an open question in
  [declaration-dsl.md](declaration-dsl.md).
- The relation-scoped fact constructors (`key`, `foreign_key`, `grain`) are the proposed
  spelling for facts that range over more than one column; the column-scoped markers
  (`PrimaryKey`, `ForeignKey`) are settled.
- The proxy result types (a `Column` yielding an aggregate or a predicate) are implied by
  the operator overloading and are not named in the design docs.
- Flag-effect constructors (`RefinementEffect` and kin) appear with varying argument
  shapes across docs; [flags_and_configs_as_types.md](flags_and_configs_as_types.md) is
  the source of record.
