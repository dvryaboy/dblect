# The algebra of domain types: which operations are safe, and what you declare to get them

*Status: design notes, theoretical foundation. This document answers one question the [declaration DSL](declaration-dsl.md) raises and does not settle: when SQL combines typed values, how does the framework know which combinations are meaningful, and what does the author have to write to make that work? The short answer is that the rules are read off the algebra of the field types the author already declares, so the surface stays small. This note grounds that claim in the literature and works it through the currency example end to end. Citations are to the primary literature; full references are listed at the end.*

## Two axes that are easy to conflate

A meaning-preserving type system for SQL has to keep two separate questions apart.

- **Validity.** Is `sum(amount) group by country` a *meaningful* operation on these values? This is what produces a finding.
- **Decomposability.** Can the result be computed from partial results, so the property propagates model-to-model along the DAG rather than being re-derived globally? This is what makes the check tractable.

They are orthogonal and we need both. The distributive/algebraic/holistic classification of Gray et al. (*Data Cube*, 1997) is the decomposability axis: `SUM`, `COUNT`, `MIN`, `MAX` are distributive (`F({Xij}) = G({F(...)})`), `AVG` is algebraic (carry a fixed-size summary, here sum and count), `MEDIAN`/`MODE`/`RANK` are holistic. Distributivity is exactly why a domain-type obligation on a `SUM` can be checked as a local propagation rule: the super-aggregate is the aggregate of partial aggregates, so the type that flows out of one model is enough to reason about the next. Validity is the rest of this note.

## What the author declares: magnitudes and tags, inferred from field algebra

The central design choice is that the author does **not** annotate which fields are quantities and which are labels. That distinction is carried by the algebra of each field's type, in the spirit of dimension types (Kennedy, *Dimension Types*, ESOP 1994) and of the much earlier observation in Galileo (Albano, Cardelli & Orsini, TODS 1985) that two values isomorphic to numbers, a weight and an age, should be made non-interchangeable by giving them distinct types with distinct operators.

A field is a **magnitude** when its values are a *quantity*: they form a commutative monoid under `+` and are scaled by a numeric scalar domain (the naturals for a count, the rationals or reals for money). This is the semimodule structure that aggregation rides (Amsterdamer, Deutch & Tannen, *Provenance for Aggregate Queries*, PODS 2011): `SUM` accumulates the `+`, `*` applies the scaling, and it is between magnitudes that tag coherence has to hold.

A field is a **tag** when its values are used by *identity* rather than as a quantity: equality is the operation that matters, and there is no numeric scalar domain (naturals, integers, rationals, reals) under which folding its values reads as a measured total. A tag type may still carry algebraic structure, since a boolean is a monoid under AND or OR and a group under XOR, and a string is a monoid under concatenation, but a logical or modular fold is not a measure, so summability never attaches to it.

Tags come in two kinds, and the difference shows up only under multiplication. A **dimensional tag** is a unit the magnitude is measured in, currency being the example: it behaves multiplicatively, so `*` and `/` do arithmetic on it (a ratio of two same-currency amounts cancels it, an exchange rate converts it). A **nominal tag** is a pure category, `contains_tax` or fiscal entity: it carries equality only, with no `contains_tax^2`. Under `+` and `sum` the two kinds behave identically (they must agree, or be held constant per group), so the coherence story below is uniform and reads "tag" for both; they part ways only when something is multiplied. Closed enumerations, booleans, and identifiers are nominal tags; currency and units of measure are dimensional.

```python
class Money(dblect.DomainType):
    amount:   Decimal(18, 2)   # Decimal adds and scales      -> magnitude
    currency: Currency         # the unit it is measured in    -> dimensional tag
```

`Decimal` adds, so `amount` is the magnitude. `Currency` is the unit it is measured in, a dimensional tag. `contains_tax: bool` would be a nominal tag, which is why adding a taxed revenue to an untaxed one is the same class of error as adding USD to EUR: under `+`, both kinds of tag demand agreement. The author writes ordinary typed fields; the classification falls out.

The classification lives with the type, so the standard library carries it: `Money`, `Count`, `Probability` are magnitudes; `Currency` and units of measure are dimensional tags; `Country`, `Identifier`, `Year`, and `contains_tax` are nominal tags. A raw numeric SQL type defaults to magnitude and a raw enum to nominal tag. The algebra is a strong default rather than a decision procedure, though: an integer is algebraically a perfect quantity even when it is really an identifier or a calendar year, both of which are tags by role, so the declared library type carries the final classification (`Identifier` and `Year` over a bare `Integer` it would be meaningless to sum). For the common cases, a `Decimal` measure or an enum label, the default is simply right, with no per-field annotation.

## A tag has three states, and two of them are not the same

The lineage from refinement types (Rondon, Kawaguchi & Jhala, *Liquid Types*, PLDI 2008, where constants carry predicates such as `3 :: {nu:int | nu = 3}`) gives the natural model: pinning a tag is narrowing a refinement, and an absent tag is the unrefined supertype. The three states an author can produce, and what each means for `sum(amount)`:

| State | How it arises | Tag set | `sum(amount)` obligation |
|---|---|---|---|
| absent | no `currency` field on `Money` | `{}` | vacuous: freely summable |
| present, pinned | `Money(currency=USD)`, or a single-currency binding | `{currency}` fixed | satisfied by construction; `MoneyUSD` still conflicts with `MoneyEUR` under `+`, union, comparison |
| present, per-row | `currency` bound to a column | `{currency}` varying | must be discharged per group |

Two type evolutions move between these, and they are genuinely different operations:

- **refine** pins a field value (`Money` to `MoneyUSD`): a subtype with fewer open tags.
- **extend** adds a field (`Money{amount}` to `Money{amount, currency}`): a richer type whose tag set grows, which activates coherence obligations that were previously vacuous. An absent field is the unrefined supertype, the type making no claim about that facet, so extension is how a claim first appears.
- **compose** unions two extensions (`Revenue{amount, contains_tax}` with `Revenue{amount, contains_shipping}` gives `Revenue{amount, contains_tax, contains_shipping}`): a field both fix must agree, the same meet the operation rules use, and a field only one carries is simply taken on. So a type that is both taxed and shipped needs no new operator.

The tag set is part of the type's identity and travels with the column through column-level lineage. That is the whole mechanism behind a declaration on one model lighting up a finding on a model nobody touched: extending the type grows the tag set, the larger tag set propagates, and an aggregation downstream that was vacuously fine now carries a live obligation.

## The operation rules

For a magnitude `m` carrying a set of tags, where a dimensional tag (a unit) does exponent arithmetic under `*` and `/`, and a nominal tag carries equality only:

| SQL | requires | produces |
|---|---|---|
| `m1 + m2`, `m1 - m2` | every tag agrees (units and categories) | same tags |
| `sum(m) group by G` | every tag constant within each group (present in `G`, pinned, or functionally determined by `G`) | same tags, now constant per group |
| `avg(m) group by G` | same as `sum` (algebraic: carry sum and count) | same tags |
| `m * k`, `m / k` with `k` dimensionless | nothing | dimensions and tags unchanged |
| `m1 * m2` | nothing forced | dimensions multiply (`money * money` is `money^2`); a nominal tag survives only if the other side lacks it, else widens to top |
| `m1 / m2` | nothing forced | dimensions divide (equal units cancel to dimensionless, a Ratio or Percentage); nominal tags as for `*` |
| `count(m)` | nothing (values are not inspected) | a tag-free `Count` |
| `m1 < m2` as a predicate | tags agree, for the comparison to mean anything | boolean |
| `min(m)`, `max(m)`, `order by m`, top-n windows | nothing forced | a real value of the type, but its tag widens to the join of the inputs (top when they differ) |
| render as money at an exposure | every tag present and single-valued | leaves the typed world |

Three kinds of reduction over a tagged magnitude behave differently, and the difference is whether the operation ignores, combines, or selects values. `count` ignores values, so it is always safe regardless of tags. `sum` and `avg` combine values into a new one, so they take the hard rule above: a varying tag corrupts the magnitude (dollars added to euros are in no currency), which is why they must be discharged. `min`, `max`, ordering, and top-n selection pick an existing value rather than synthesizing one, so the magnitude they return is real; only its tag is uncertain, because the comparison that chose it was tag-blind. They therefore widen the result tag to the join of the inputs (top when the inputs disagree) rather than failing at the operation, and that widened tag is caught by the ordinary checks wherever a definite tag is later required: assignment to a typed column, a later combine, or rendering at an exposure. The same discharges that make a `sum` sound (the tag in the group key, pinned, or functionally determined) make the selection meaningful too, since they hold the tag constant across what is compared.

Multiplication and division are the generic part, and they are why a dimensional tag is worth separating from a nominal one. They are the operations of the free abelian group of units (Kennedy): `*` adds unit exponents and `/` subtracts them, with no per-case knowledge beyond the operands' own dimensions. `money<usd> / money<usd>` cancels to a dimensionless ratio; an `ExchangeRate` typed `eur/usd` times a `MoneyUSD` gives `MoneyEUR`, the `usd` exponents cancelling; and `money * money` is `usd^2`, which is not an error at the multiply but a well-typed value nobody usually wants, flagged only where a `usd^2` is later used as money. A reversed conversion is caught for free this way: multiplying by a rate typed the wrong direction (`usd/eur`) yields `usd^2 eur^-1`, which the same downstream check flags. A nominal tag has no exponents, so it simply rides through a scalar multiply (`revenue * 0.9` keeps its tax status) and widens to top if two nominally-tagged operands are multiplied together.

The aggregation rule is summarizability (Lenz & Shoshani, *Summarizability in OLAP and Statistical Data Bases*, SSDBM 1997): the validity of `sum ... group by` rests on the aggregation function being type-compatible with the measure and with the category aggregated over. Summing a magnitude across a varying tag is the type-incompatible case. The `country -> currency` discharge is reasoning about summarizability under a declared dimension dependency (Hurtado & Mendelzon, ICDT 2001).

## Where the group key comes from

The rule above is written over an abstract group key `G`, the set of dimensions a reduction holds constant. SQL produces that key in more than one syntactic form, and each maps onto the same obligation, so the discharge paths carry over unchanged.

- **`GROUP BY`** is the direct case: `G` is the grouping expression list.
- **Windowed aggregates** carry the key in the window spec. `sum(amount) over (partition by ...)` reduces within each partition, so the `PARTITION BY` list plays the role of `G`, and the same tag-coherence obligation applies per partition. The substrate already reads a window's partition and order context as first-class structure, so the check reuses it rather than treating windows as opaque. The empty-partition case (`over ()` or a frame with no `PARTITION BY`) reduces over the whole relation, which is `G = {}`, the strictest obligation.
- **`ROLLUP`, `CUBE`, and `GROUPING SETS`** compute several group keys at once and stack the results, with the higher-level subtotal and grand-total rows carrying `NULL` in the dimensions they reduced away. Each grouping set is its own `G`, so a tagged magnitude is well-typed only when the obligation holds at *every* set in the stack, the grand total (`G = {}`) included. The `NULL`-padded subtotal rows interact with the nullability property the substrate already runs: a reduced-away dimension reads as `NULL` by construction there rather than as a missing tag, so the two should be reconciled rather than double-reported. The precise treatment of multi-grain stacking is left to settle against a real cube-shaped mart; until then the conservative reading is to require the obligation at the coarsest set, which subsumes the finer ones.

The frame clause (`rows between ...`) raises the semi-additive question rather than the tag question: a running or trailing-window reduction over a level measure has the same hazard as summing it across time, and is discharged the same way (use the reduction the measure supports along that dimension).

## What holds a dimension

A dimension is an element of the free abelian group over units: a normalized map from unit to integer exponent, with zero exponents dropped so the empty map is dimensionless and equality is map equality.

```python
Unit      = Concrete[str] | PerRow[ColumnRef]   # "USD", or the currency column travelling with this amount
Dimension = FrozenMap[Unit, int]                # {USD: 1}; money^2 is {USD: 2}; a rate is {EUR: 1, USD: -1}; {} is dimensionless
```

`money^2` needs no special type; it is the point `{USD: 2}`, held by the same structure that holds `money` (`{USD: 1}`), a per-dollar rate (`{USD: -1}`), and the variance of money (`{USD: 2}`). The operations are the group operations: `*` merges the maps adding exponents then drops zeros, `/` subtracts, `==` is map equality.

The one twist past textbook units is that a unit's *identity* can be per-row. A single-currency amount is `{Concrete("USD"): 1}`; a per-row multi-currency amount is `{PerRow(currency_col): 1}`. Cancellation then works by identity: `PerRow(c) / PerRow(c)` cancels because it is the same column reference, while two different currency columns do not. And summing across a `PerRow(c)` unit is sound only when `c` is constant over each group, which is exactly the coherence obligation discharged by the group key or a functional dependency. So the dimensional representation and the coherence rule are one thing: a per-row unit must stay invariant wherever values are folded, or it stops being a single unit.

The full value attached to an amount column is three parts, since nominal tags do not belong in the group (there is no `contains_tax^2`):

```python
@dataclass(frozen=True)
class MagnitudeType:
    base:      SqlType                       # Decimal(18, 2)
    dimension: Dimension | Top               # {USD: 1}; money^2 is {USD: 2}; Top if mixed or unknown
    nominal:   FrozenMap[str, object | Top]  # {contains_tax: False, ...}  -- categorical, equality only
```

`Top` is reached when a dimension stops being single-valued: summing across currencies, unioning `{USD: 1}` with `{EUR: 1}`, a min or max across differing units, or passing through an opaque function. So the lattice the substrate runs is flat over the group: each distinct known monomial is its own incomparable point, all under `Top`, with `*` and `/` operating inside a known value and `meet`/`join` working over the knowledge (equal dimensions agree, unequal join to `Top` or conflict on grounding). A literal sits at bottom, polymorphic, until context fixes it.

## Functions and UDFs

Operators (`+ - * /`, comparison, the standard aggregates) carry fixed transfer rules and need no per-case knowledge, so they are generic. Everything else is a function whose effect on dimensions comes from a **signature**. By dimensional homogeneity (Buckingham 1914) a signature's result is a monomial in the argument dimensions (or a constant), and anything that cannot be written that way, the transcendentals, is defined only on dimensionless arguments. Signatures live in one registry: built-in functions are the entries the framework ships (keyed by sqlglot expression type), a custom UDF is an entry the author adds (keyed by a resolved function reference), a dbt macro never reaches the registry because it expands to SQL and is propagated through transparently, and an unknown function returns `Top` with a coverage note. The signature is unit-polymorphic and rides Pydantic generics, so the headline case reads `convert(Money[U], Rate[V, U]) -> Money[V]`.

The signature object, the single registry, call-site resolution by unit unification, the override and missing-function rules, and the extension surface are specified in [domain-type-functions.md](domain-type-functions.md).

## How it lands on the charge example

1. **Before.** `Money{amount}`, `T = {}`. `sum(charge_amount) group by country` requires `{}`, so it is valid, and SUM's distributivity (Gray et al.) lets the resulting type propagate downstream as a local rule.
2. **The PR extends the type.** `Money{amount, currency}`, with `currency` bound per-row on the source. `T = {currency}`, varying. The tag set rides column-level lineage to the downstream `charge_amount` reference, even though the `currency` column was projected away before the rollup.
3. **The sum lights up.** `sum(charge_amount) group by country` now requires `currency` constant per group. No discharge path is open: it is not in the `GROUP BY`, not pinned, has no declared `country -> currency`, and is not filtered. A magnitude is being summed across a varying tag, which is the summarizability violation, and the framework flags it, pointing back at the declaration that grew the tag set.
4. **The good case stays quiet.** A `tip_amount / charge_amount` ratio with matching currency tags cancels to a dimensionless Percentage, valid with no annotation, so the check does not cry wolf on correct same-currency arithmetic.
5. **Discharges, each with a basis.** Add `currency` to the `GROUP BY` (tag constant per group), declare `country -> currency` (dependency discharge), `WHERE currency = 'USD'` (pins the tag), or convert through a typed exchange rate before summing (the multiplicative fragment).

## Discharging an aggregation

An aggregation obligation has exactly three discharge paths, and they are worth stating as one rule because they are the only ways a `sum(m) group by G` over tag set `T` becomes valid. For every tag `t` in `T`:

- `t` is in the group key `G`, so the tag is constant per group by construction, or
- `t` is pinned in the type, so it is constant everywhere, or
- `G` functionally determines `t` (`G -> t`), so each group, fixed on `G`, admits one value of `t`.

The third path is the interesting one, because it licenses summing a tagged magnitude without carrying the tag at all. `country -> currency` means equal `country` implies equal `currency`: each country uses one currency. The groups in `sum(charge_amount) group by country` are keyed by `country`, so within any group the currency is single-valued, and the sum stays within one currency even though the `currency` column was projected away upstream. The group key recovers the constancy the dropped column would have supplied. This is summarizability under a dimension dependency (Hurtado & Mendelzon, ICDT 2001).

The discharge is local and the tag survives. Each group's result is a `Money` whose currency is the currency of that country, so the output is not globally single-currency; its `currency` is now functionally determined by `country`. A later `sum(total_amount)` across countries lights up again, correctly, since that aggregation has its own undischarged obligation. A functional dependency buys one sound aggregation, not blanket permission.

A functional dependency is a checkable claim, so it sits in the stronger trust class rather than being a bare assertion. The framework trusts it at analysis time to discharge the sum, and does not prove it. Because it is checkable, a guard that runs against data, the same kind that backs a declared key, can contradict it by finding the dependent tag is not single-valued per key. A declared dependency that does not hold, a country that billed in two currencies after a switchover, then becomes its own finding rather than silently licensing the currency-mixing it was meant to permit; where no such guard runs, the discharge is flagged as resting on an unverified dependency. Often the dependency need not be declared at all: when `currency` arrives by a join to a dimension whose key is `country`, the dependency is structural and is inferred from the join, reading the dimension's key the way an existing `unique` test or primary key is read. The inference rests on that uniqueness: a dimension with several rows per country is not keyed on `country`, and the join fans out, which the cardinality property flags rather than reading as a dependency.

## Semi-additive magnitudes: the third aggregation hazard

Tag coherence and grain are two preconditions for a sound `sum`. A third applies to a particular kind of magnitude. A flow, such as revenue or quantity sold, is additive over every dimension: it makes sense to sum it across customers, across products, and across time. A level, such as an account balance, inventory on hand, or headcount, is additive over entity dimensions but not over time, because summing balances across months adds snapshots of the same stock and produces a figure that means nothing. This is the additive, semi-additive, and non-additive taxonomy of dimensional modeling (Kimball), and it is the measure-versus-category type compatibility of summarizability (Lenz & Shoshani) seen from the measure side rather than the tag side.

The obligation has the same shape as the others. A `sum(m) group by G` reduces over every dimension absent from `G`, so summing a semi-additive magnitude over a dimension it is not additive along is the violation. A monthly balance summed without `month` in the group key collapses time and adds snapshots. The discharges are to keep that dimension in the grain, or to use the reduction the measure actually supports along it, such as last-value or average over time rather than `sum`.

The difference from currency is what the author must declare. A tag needs nothing beyond its field type, because its algebra already says it is equality-only. A semi-additive magnitude carries information its base type does not: which dimensions it is additive over. So this is the one place the algebra needs an annotation the currency case does not. The concrete authoring surface for it is deferred until a real level measure is in front of us to settle it against; the all-or-nothing `summable` flag is the degenerate case of the dimension-scoped form, where the set of additive dimensions is either everything or nothing.

## Joins: keys, grain, and tags

A join pairs rows; it does not add magnitudes. So a join carries no tag-coherence obligation of its own, and the arithmetic that follows a join reuses the rules above. What a join does change is which rows exist and how many times each magnitude appears, and that introduces a second integrity axis alongside tag coherence.

**Grain.** The grain of a relation is what one row stands for, identified by its key. A magnitude is summable only when the rows being folded are distinct at the grain that produced it, so that each underlying value is counted once. This is the disjointness condition of summarizability (Lenz & Shoshani) read through the key.

**Fan-out is the grain hazard.** Joining a one-row-per-order table that carries `order_total` to a many-rows-per-order line-items table replicates each `order_total` once per line item. A later `sum(order_total)` then counts each order's total several times. This is the fan trap of dimensional modeling (Kimball), and it is a grain violation rather than a tag violation: the currencies all agree, but the values have been duplicated. The rule: `sum(m)` is sound only when `m`'s origin key is still a key of the relation being summed. A join that does not preserve that key has fanned the magnitude out, and summing it double counts.

The four obligations a join raises, and the integrity axis each serves:

| Join concern | Obligation | Axis |
|---|---|---|
| join key types | the `ON` equality is a comparison, so its two sides' tags must agree (joining an ISO-2 `Country` to an ISO-3 one, or a `MoneyUSD` amount to a `MoneyEUR` amount, is a finding) | tag coherence |
| fan-out | a magnitude summed downstream must have its origin key preserved through every join on the path | grain |
| dependency flow | a join to a dimension keyed on `country` creates `country -> currency`; an inner join carries an existing dependency through; a fan-out can break one | discharge of later aggregations |
| outer-join misses | a `LEFT` join miss yields a NULL tag, which is an unknown currency and must block a sum until resolved | tag coherence |

Tag coherence and grain are the two preconditions for a sound `sum`: every contributing value is in one currency, and every contributing value is counted once. The currency example exercises the first; fan-out exercises the second. Both are the same underlying principle, that meaning-bearing structure must survive whenever SQL folds many rows into one, applied to two different kinds of structure.

## The lattice underneath

All of the above is one structure: a lattice of tag knowledge attached to a base type, with operations stated as require/produce over that lattice and safety decided by the lattice order. This is the type-qualifier view (Foster, Fähndrich & Aiken, *A Theory of Type Qualifiers*, PLDI 1999) resting on Denning's lattice model of information flow (Denning, CACM 1976), and it is the same meet-semilattice machinery the substrate already runs for nullability and uniqueness, which is why domain type drops in as one more property over [lineage-facts.md](lineage-facts.md) rather than a separate engine. For currency the qualifier lattice is

```
        T   (unknown or mixed)        <- detached, or summed across currencies
       /|\
   USD EUR GBP ...                    <- a known single currency
       \|/
        _|_ (dimensionless)           <- a ratio where the currency cancelled
```

That picture is the single-unit slice. The general structure is the flat lattice over the free abelian group of units described in "What holds a dimension": each known monomial is its own point, all under `T`, which carries `usd^2`, `eur/usd`, and the rest without enumerating them. A detached amount (projected away from its currency) is the same base type at qualifier `T`; an operation is safe exactly when the operand qualifiers meet its requirement. The modern algebraic backbone for the aggregation case is semiring annotation (Green, Karvounarakis & Tannen, *Provenance Semirings*, PODS 2007) and its semimodule extension for aggregates (Amsterdamer, Deutch & Tannen, PODS 2011). The mutually-commutative-aggregate condition of Abo Khamis, Ngo & Rudra (*FAQ: Questions Asked Frequently*, PODS 2016) states precisely when stacked aggregations may be interchanged, and the same provenance line carries into update exchange (Green, Karvounarakis, Ives & Tannen, *Update Exchange with Mappings and Provenance*, VLDB 2007).

## What this commits the design to

- The author declares fields with their natural types and nothing else for the common case. Magnitude versus tag is inferred from field algebra, so there is no `axis` or `tag` keyword.
- Absence and presence of currency are the absent / pinned / per-row states of the `currency` field. The before-and-after of the example is the extend operation growing the tag set, which is why the obligation is retroactive and reaches undeclared models.
- The only extra declarations the author ever adds are functional dependencies to discharge an aggregation (`country -> currency`) and, for the separate semi-additive-measure problem a balance over time would raise, the dimensions a magnitude is additive over. Currency needs neither.
- Multiplication and division are generic group arithmetic on units, so they need no per-expression semantics. Functions get their dimensions from a shipped catalog of built-ins, transparently from expanded macros, and from a declared signature (or a conservative `Top`) at an opaque UDF, with one power-user hook to annotate a built-in or a custom function.

## Open questions

- **Inference overrides.** The magnitude/tag inference is right when the right library types are used, and the `Year`- or identifier-as-`Integer` trap shows raw numeric types can mislead it (both are algebraically quantities but tags by role). Whether the framework should warn on summing a bare `Integer` dimension, or require dimensions and identifiers to be tag-typed, wants a real schema to decide.
- **Tag-blind comparison and ordering.** The working resolution above treats `min`, `max`, and ordering as value-selecting rather than value-combining: the result is a real value, its tag widens to top when the inputs disagree, and the widened tag is caught later where a definite tag is required, consistent with the naked-amount taint. This keeps the operation quiet and reuses existing machinery. The residual blind spot is the same as elsewhere, a top-tagged value that flows only into further untagged computation and never reaches a typed column, a combine, or an exposure. Whether some uses (a definitive "cheapest charge" surfaced directly to a user) deserve an eager finding rather than the lazy taint is the part left open.
- **Semi-additivity surface.** The hazard is stated above as the third aggregation precondition, and the obligation and discharges follow the same shape as the others. What stays open is only the authoring surface: how a magnitude declares the dimensions it is additive over (the dimension-scoped general form, of which a bare `summable` flag is the degenerate case). This should be designed against a real level measure rather than invented ahead of one.
- **Dimensionless is coarse.** A tax rate and an unrelated ratio are both dimensionless, so the unit layer accepts `money * either`. Catching a wrong dimensionless factor is the refinement and nominal-tag layer's job, not the unit layer's, and how much to invest there is open.
- **Multi-grain stacking.** `ROLLUP`, `CUBE`, and `GROUPING SETS` raise the obligation at several group keys at once, with `NULL`-padded subtotal rows that overlap the nullability property. The conservative reading (require the obligation at the coarsest set) is stated above; the precise per-set treatment and its reconciliation with nullability want a real cube-shaped mart to settle.
- **Function signatures.** The catalog of built-in signatures, the extension surface for custom functions, call-site resolution, and the open spellings around them are specified in [domain-type-functions.md](domain-type-functions.md).

## Lenient and strict modes

The produce rules above resolve a no-claim (`Top`) operand by widening rather than
flagging: where the analyzer cannot discharge an operator's agreement requirement
because one side carries no tag, it makes no claim about the result instead of calling
the expression an error. This is the lenient default, and it is the right one for a
codebase being onboarded, where most columns are still untagged and an eager finding on
every contact with an untyped magnitude would bury the real ones.

A future strict mode would keep the same lattice and the same produce rules everywhere
the operands carry claims, and differ mainly in how it treats a `Top` operand under an
operator that requires agreement: where lenient widens, strict raises a finding, on the
reading that the author was obliged to tag every magnitude that reaches such an operator.
One further divergence fires earlier, at declaration time rather than over a `Top`
operand: whether a bare integer is accepted at all. The divergence points, recorded as
they are found so the mode is a coherent switch rather than a scatter of flags:

- **Additive `Top` operand.** `money + untagged` (and `-`). Lenient produces `Top` (the
  sum carries no dimensional claim, no finding). Strict treats adding an un-dimensioned
  magnitude to a dimensioned one as the finding, since the addend's unit was required and
  absent. This is the one the rescaling PBT exercises: holding the untagged column fixed
  under the group action witnesses that the lenient `Top` is the only sound claim, and a
  rule that instead inherited the dimensioned side's unit is unsound.
- **Comparison against a `Top` operand.** `money = untagged`, ordering, join-key equality.
  Lenient produces a tag-free boolean and asks nothing of an untagged side. Strict flags
  comparing a dimensioned value against an un-dimensioned one, the same obligation the
  join-key-types row raises for two *disagreeing* known tags, extended to the no-claim
  side.
- **Tag-blind `min`/`max` selection.** A selecting aggregate picks an existing value by a
  comparison, so `min(amount)`/`max(amount)` over a group whose currency is not held
  constant chooses the smallest or largest *number* across mixed currencies. Lenient
  treats the result as the real value it is and widens its tag to top (the
  selection rule above), saying nothing at the operation. Strict raises a finding: this is
  the ordering-against-a-`Top`-operand divergence applied to the comparison a selection is
  built on, not a separate flag. The classification that names `min`/`max` as selecting is
  in `dblect.sql.aggregates`.
- **Bare-integer field declaration.** A bare `int` / `Integer` / `BigInt` is algebraically
  a perfect quantity yet by role as often an identifier or a calendar year, both tags, so
  its role is the one a scalar's algebra does not settle. Lenient classifies it as inert,
  making no claim either way (a measure is spelled `Count` / `Decimal`, an identifier or
  year carries its domain type). Strict rejects it at the declaration and teaches the
  choice, on the reading that the author was obliged to name the role rather than leave a
  warehouse integer to default. This is the one divergence that escalates an *acceptance*
  rather than a widening, and the only one that fires when a contract is read rather than
  when an expression is typed; the classifier is `dblect.types.scalars`.

Multiplication and division are deliberately absent from this list, but for a different
reason than addition. `*` and `/` carry no agreement requirement at all (any two units
compose), so a `Top` operand is never a requirement violation, in either mode. It is
still absorbing: a no-claim factor may carry hidden units (a widened sum such as
`c0 + c1` is exactly such a value), so the product's dimension is unknown rather than the
other operand's unit. The rescaling PBT surfaced this: claiming `(c0 + c1) * c1` is clean
`usd` breaks the law, because the naked factor is not a rescaling-invariant scalar.

The ordinary scalar case (`amount * 0.9`) is kept typed by a separate distinction rather
than by waving `Top` through: a bare numeric literal is *polymorphic*, not no-claim. A
polymorphic literal takes its unit from context, acting as the identity under `+`/`-` (so
`amount + 5` stays the amount's currency) and as a dimensionless factor under `*`/`/` (so
`amount * 0.9` keeps it). This is the algebra's "a literal sits at bottom, polymorphic,
until context fixes it", and it is what separates a known scalar from an untagged
magnitude, the two having been conflated as `Top` before. A further may/must refinement
that would let a widened sum still carry a *provisional* unit (so `(usd + naked) + eur`
could be recovered as a conflict, a completeness gain rather than a soundness fix) is left
as a follow-up.

## References

- Albano, A., Cardelli, L., & Orsini, R. (1985). Galileo: A Strongly-Typed, Interactive Conceptual Language. *ACM Transactions on Database Systems*, 10(2), 230-260.
- Amsterdamer, Y., Deutch, D., & Tannen, V. (2011). Provenance for Aggregate Queries. *PODS*.
- Abo Khamis, M., Ngo, H. Q., & Rudra, A. (2016). FAQ: Questions Asked Frequently. *PODS*.
- Denning, D. E. (1976). A Lattice Model of Secure Information Flow. *Communications of the ACM*, 19(5), 236-243.
- Foster, J. S., Fähndrich, M., & Aiken, A. (1999). A Theory of Type Qualifiers. *PLDI*.
- Gray, J., Chaudhuri, S., Bosworth, A., Layman, A., Reichart, D., Venkatrao, M., Pellow, F., & Pirahesh, H. (1997). Data Cube: A Relational Aggregation Operator Generalizing Group-By, Cross-Tab, and Sub-Totals. *Data Mining and Knowledge Discovery*, 1(1), 29-53.
- Green, T. J., Karvounarakis, G., & Tannen, V. (2007). Provenance Semirings. *PODS*.
- Green, T. J., Karvounarakis, G., Ives, Z. G., & Tannen, V. (2007). Update Exchange with Mappings and Provenance. *VLDB*.
- Hurtado, C. A., & Mendelzon, A. O. (2001). Reasoning about Summarizability in Heterogeneous Multidimensional Schemas. *ICDT*.
- Kennedy, A. J. (1994). Dimension Types. *ESOP*. See also Kennedy, A. J. (1996), *Programming Languages and Dimensions* (PhD thesis, University of Cambridge), and Kennedy, A. J. (2009), Types for Units-of-Measure: Theory and Practice, *CEFP*.
- Kimball, R., & Ross, M. (2013). *The Data Warehouse Toolkit: The Definitive Guide to Dimensional Modeling* (3rd ed.). Wiley. (Additive, semi-additive, and non-additive measures.)
- Lenz, H.-J., & Shoshani, A. (1997). Summarizability in OLAP and Statistical Data Bases. *SSDBM*.
- Rondon, P. M., Kawaguchi, M., & Jhala, R. (2008). Liquid Types. *PLDI*.
