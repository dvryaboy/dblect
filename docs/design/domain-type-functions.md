# Function dimensional signatures: the catalog and how to extend it

*Status: design notes. The [algebra](domain-type-algebra.md) establishes that operators are generic and every other function needs a dimensional signature; this document defines what a signature is, the single registry that holds them, how a call site is resolved, and the surface a power user extends. The signature notation here is the target shape; the exact spelling is the open part and is listed at the end. Citations are to the primary literature, with full references at the end.*

## What a signature is

A function's effect on dimensions is not free to be anything. Dimensional homogeneity (the Buckingham Pi theorem, Buckingham 1914) says a dimensionally sound function's result is a **monomial in its argument dimensions**, a product of those dimensions raised to rational powers, times a constant dimension. A function that cannot be written that way, anything whose series mixes powers such as `exp`, `ln`, or `sin`, is defined only on **dimensionless** arguments. That is the whole constraint, and it gives the signature two parts:

- **per-argument constraints**: an argument either contributes a unit variable (its dimension is bound and may appear in the result), or is required to be dimensionless, or is required to share a dimension with another argument;
- **a result**: a dimension expression over the bound unit variables (products, quotients, integer or rational powers) or a constant dimension.

Everything else people say about function dimensions is a special case. "Preserves the dimension" is `result = arg`. "Squares it" is `result = arg^2`. "Halves it" is `result = arg^(1/2)`. "Requires dimensionless" is the transcendental case. So the principled object is the monomial signature, and the named behaviors are shorthand over it rather than a fixed vocabulary.

## Authoring a signature

The signature is unit-polymorphic, which is parametric polymorphism over units (Kennedy 1994), so the natural Python surface is a generic over a unit variable. We borrow the *shape* of Pydantic's generics, the parameterized-class syntax and the editor support that rides on it, and interpret the unit algebra ourselves: a `unit_var` is our own object, and an annotation like `Quantity[U ** 2]` is read as a unit monomial rather than evaluated by Python's type system. The borrowed surface keeps the authoring experience familiar; the interpretation layer is where the dimensional reasoning lives. A domain type is generic over its unit, and a unit variable plays the role a `TypeVar` plays for ordinary generics:

```python
from dblect import unit_var
from dblect.types import Quantity, Money, Rate, Dimensionless

U = unit_var("U")
V = unit_var("V")
```

A signature is then a typed stub whose annotations carry the whole thing. The arguments give the constraints, the return annotation gives the result monomial, and the result monomial is written with ordinary Python operators on the unit variables:

```python
@dblect.signature("convert_currency")
def _(amount: Money[U], rate: Rate[V, U]) -> Money[V]: ...      # the headline: USD * (EUR/USD) -> EUR

@dblect.signature("portfolio_variance")
def _(x: Money[U]) -> Quantity[U ** 2]: ...                     # money -> money^2

@dblect.signature("geomean_ratio")
def _(a: Quantity[U], b: Quantity[U]) -> Dimensionless: ...     # equal units required, result dimensionless
```

Three things make this clean rather than a notation to memorize:

- **The equal-argument constraint falls out of generics.** Writing `U` on two arguments forces them to share a dimension, exactly as reusing a `TypeVar` unifies two parameters. `greatest`, `least`, `coalesce`, and `+` are all just `(Quantity[U], Quantity[U]) -> Quantity[U]`. There is no separate "must match" annotation to learn.
- **The result monomial is just operators on unit variables.** `U ** 2`, `U * V`, and `U / V` are read off the return annotation. We never evaluate them as Python; we interpret the unit-variable algebra. `Rate[V, U]` is the library's name for `Quantity[V / U]`.
- **Dimensionless is a value, not a special case.** A transcendental is `(Dimensionless) -> Dimensionless`, and a function that emits a fixed dimension regardless of input simply names it in the return.

A few of the most common shapes (preserve, product, quotient) recur often enough that a named shorthand can stand in for the stub, but the stub is the principled form and the shorthand is sugar, never the other way around.

## One registry, two keys

There is a single registry from a function to its signature. The split that matters is not built-in versus UDF, it is **how the function is identified**, and there are two identifiers:

- **A typed AST node.** sqlglot parses known functions into typed expression classes (`exp.Abs`, `exp.DateTrunc`, `exp.Variance`), so the entries we ship are keyed by `type[exp.Func]`, dispatched the way `functools.singledispatch` dispatches on a type. No strings. This is the same shape as sqlglot's own `annotate_types` pass, which already keys a function-to-return-type table by expression class, so we are mirroring a mechanism that already lives in the stack.
- **A resolved function reference.** A warehouse UDF parses as `exp.Anonymous` carrying a name, which is an external identifier we do not own. We wrap it in a `FunctionRef(database, schema, name)` resolved against the dbt manifest and warehouse catalog, so a misspelled function is a finding rather than a silent miss, exactly as `dbt_model = "marts.fct_orders"` resolves. The name is irreducibly a string because it names something outside our world, but it is a checked reference, and the signature attached to it is fully typed.

So "built-in versus UDF" dissolves into "registry hit versus miss." Built-ins are the entries we ship, UDFs are entries a user adds, a dbt macro never reaches the registry at all because it expands to SQL before analysis and is propagated through transparently, and an unknown function is simply a miss.

Built-ins are also dialect-scoped, since each warehouse has its own function set and sqlglot normalizes many dialect spellings to one expression class. dbt tells us the adapter, so we load the right dialect's entries. UDF overloads are resolved by name and arity, refined by argument dimensions only where a project actually overloads.

## Resolving a call site

Given a function call with known argument dimensions, resolution is: look the function up in the registry by AST type or resolved reference; match each argument dimension against the signature's argument pattern, binding unit variables; check the dimensionless and shared-variable constraints; then evaluate the result expression under the bindings to get the call's dimension. The matching step is unification over the free abelian group of units, which Kennedy (1994, 1996) showed is decidable with principal types, so it terminates and yields a most general result. A constraint violation (a transcendental handed money, two unequal units where the signature shares a variable) is the finding; the result otherwise flows on as an ordinary dimension.

## Extending, overriding, and missing

- **Extending.** A user registers a signature through the same decorator into the same registry, for a custom UDF or for a built-in our shipped catalog did not cover. There is one mechanism, not a separate internal and external API.
- **Overriding.** A user signature for a function we already ship takes precedence, which is the same trust ordering used elsewhere: the shipped catalog is a default, a user declaration is vouched and wins. Because a silent override of a correct shipped signature is a footgun, the override is reported, and a genuine conflict between the two is surfaced rather than swallowed. The legitimate case this serves is fixing a shipped signature that is wrong or stale for a particular warehouse.
- **Missing.** An unknown function returns `Top` for its result dimension and drops nominal tags, which is sound but stops checking through that call. This is only visible when a typed magnitude enters it; an unknown function over untyped columns stays silent. When a typed value does enter an unannotated function, a low-severity note records that coverage lapsed there, so the gap is visible rather than silent, and the fix is one annotation. We deliberately avoid a "default to preserve" heuristic for unknown unary functions, since a custom `to_cents` or `usd_to_eur` would slip through, and those are exactly the conversions the dimensional model exists to catch.

Nominal tags are governed separately from this catalog: a structure-preserving function carries them through (the absolute value of a taxed revenue is still taxed), and the rare function that changes a categorical meaning would say so. Signatures here are about dimensions.

## Devex precedents we are drawing on

This surface sits in a well-developed neighborhood, and we are happy to borrow.

- **Pydantic generics** give us the `Money[U]` surface directly. Parameterizing a model over a variable and reading those annotations back is exactly what Pydantic already does well, so the authoring syntax and editor support come for free; the unit algebra those annotations carry is then ours to interpret.
- **Pandera** has two registry mechanics worth knowing: `register_check_method` (Bantilan 2020), a decorator that registers a named check into a global registry for later use by name, and its pluggable dtype `Engine`, where a user registers a custom logical dtype with equivalence and coercion. Both are clean models for a single-decorator extension point feeding one registry. We take that spirit. Our entries are richer than a named check, since they carry a unit-polymorphic signature, so we do not hold to Pandera's exact mechanics, but its developer experience is the bar to clear.
- **sqlglot's `annotate_types`** is the closest in-stack precedent for the catalog itself, a function-to-return-type table keyed by expression class, and our dimension catalog is the same shape one level up.
- **Pint** (the Python units library) and the units-of-measure tradition (Kennedy; the Frink language) are where the dimensional algebra and unit unification come from.

## Open questions

- **The exact spelling.** The typed-stub-with-unit-variables shape is the target, but the surface details are open: how a unit variable is declared and bound, whether `Rate[V, U]` or `Quantity[V / U]` is the canonical way to write a composite unit, and which named shorthands (if any) ship alongside the stub form.
- **Overload resolution depth.** Matching by name and arity covers most projects; whether to dispatch on full argument dimensions for genuinely overloaded UDFs waits on a project that overloads.
- **Fractional exponents.** `SQRT` of a non-square dimension yields a half-integer exponent the integer-exponent group cannot hold. Whether to widen such a result to `Top`, carry rational exponents, or flag it is unsettled and waits on a real case.
- **Catalog scope.** Which dialects' built-in sets to ship first, and how much to lean on sqlglot's normalization versus per-dialect entries, is a coverage question to settle against real projects.

## References

- Bantilan, N. (2020). pandera: Statistical Data Validation of Pandas Dataframes. *Proceedings of the 19th Python in Science Conference (SciPy)*.
- Buckingham, E. (1914). On Physically Similar Systems; Illustrations of the Use of Dimensional Equations. *Physical Review*, 4(4), 345-376.
- Kennedy, A. J. (1994). Dimension Types. *ESOP*. See also Kennedy, A. J. (1996), *Programming Languages and Dimensions* (PhD thesis, University of Cambridge).
