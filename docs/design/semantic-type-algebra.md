# The algebra of semantic types: which operations are safe, and what you declare to get them

*Status: design notes, theoretical foundation. This document answers one question the [declaration DSL](declaration-dsl.md) raises and does not settle: when SQL combines typed values, how does the framework know which combinations are meaningful, and what does the author have to write to make that work? The short answer is that the rules are read off the algebra of the field types the author already declares, so the surface stays small. This note grounds that claim in the literature and works it through the currency example end to end. Where a primary is present in the local paper corpus it is cited by filename; where the canonical reference sits outside that corpus it is named as an external reference.*

## Two axes that are easy to conflate

A meaning-preserving type system for SQL has to keep two separate questions apart.

- **Validity.** Is `sum(amount) group by country` a *meaningful* operation on these values? This is what produces a finding.
- **Decomposability.** Can the result be computed from partial results, so the property propagates model-to-model along the DAG rather than being re-derived globally? This is what makes the check tractable.

They are orthogonal and we need both. The distributive/algebraic/holistic classification of Gray et al. (`1996_cs_0701155_Data_Cube_A_Relational_Aggregation_Operator_Generalizing_Gro.pdf`) is the decomposability axis: `SUM`, `COUNT`, `MIN`, `MAX` are distributive (`F({Xij}) = G({F(...)})`), `AVG` is algebraic (carry a fixed-size summary, here sum and count), `MEDIAN`/`MODE`/`RANK` are holistic. Distributivity is exactly why a semantic-type obligation on a `SUM` can be checked as a local propagation rule: the super-aggregate is the aggregate of partial aggregates, so the type that flows out of one model is enough to reason about the next. Validity is the rest of this note.

## What the author declares: magnitudes and tags, inferred from field algebra

The central design choice is that the author does **not** annotate which fields are quantities and which are labels. That distinction is carried by the algebra of each field's type, in the spirit of dimension types (Kennedy, *Dimension Types*, ESOP 1994; external to this corpus) and of the much earlier observation in Galileo (`1985_958b42ead9beb89134fd5191635c72dd5c58c9fb_GALILEO_a_strongly-typed_interactive_conceptual_language.pdf`) that two values isomorphic to numbers, a weight and an age, should be made non-interchangeable by giving them distinct types with distinct operators.

A field is a **magnitude** when its type is additive: its values form a commutative monoid under `+` (a group, with negatives) and can be scaled by dimensionless numbers. This is the semimodule structure that aggregation rides (Amsterdamer, Deutch & Tannen, *Provenance for Aggregate Queries*, PODS 2011; external).

A field is a **tag** when its type supports equality but no meaningful addition: a closed enumeration, a boolean, an identifier.

```python
class Money(dblect.SemanticType):
    amount:   Decimal(18, 2)   # Decimal adds and scales -> magnitude
    currency: Currency         # enum, equality only      -> tag
```

`Decimal` adds, so `amount` is the magnitude. `Currency` is a closed enum with no addition, so `currency` is a tag the magnitude must stay coherent with. `contains_tax: bool` is a tag for the same reason, which is why adding a taxed revenue to an untaxed one is the same class of error as adding USD to EUR. The author writes ordinary typed fields; the classification falls out.

The classification lives with the type, so the standard library carries it: `Money`, `Count`, `Probability` are magnitudes; `Currency`, `Country`, `Identifier`, `Year` are tags. A raw numeric SQL type defaults to magnitude and a raw enum to tag, with the nudge that a dimension such as a calendar year wants a tag-typed library type (`Year`) rather than a bare `Integer` it would be meaningless to sum. This keeps the inference honest without a per-field annotation.

## A tag has three states, and two of them are not the same

The lineage from refinement types (`2008_285bd55c6a7f4a1e829ecf5cd380900070ca6223_Liquid_types.pdf`, where constants carry predicates such as `3 :: {nu:int | nu = 3}`) gives the natural model: pinning a tag is narrowing a refinement, and an absent tag is the unrefined supertype. The three states an author can produce, and what each means for `sum(amount)`:

| State | How it arises | Tag set | `sum(amount)` obligation |
|---|---|---|---|
| absent | no `currency` field on `Money` | `{}` | vacuous: freely summable |
| present, pinned | `Money(currency=USD)`, or a single-currency binding | `{currency}` fixed | satisfied by construction; `MoneyUSD` still conflicts with `MoneyEUR` under `+`, union, comparison |
| present, per-row | `currency` bound to a column | `{currency}` varying | must be discharged per group |

Two type evolutions move between these, and they are genuinely different operations:

- **refine** pins a field value (`Money` to `MoneyUSD`): a subtype with fewer open tags.
- **extend** adds a field (`Money{amount}` to `Money{amount, currency}`): a richer type whose tag set grows, which activates coherence obligations that were previously vacuous.

The tag set is part of the type's identity and travels with the column through column-level lineage. That is the whole mechanism behind a declaration on one model lighting up a finding on a model nobody touched: extending the type grows the tag set, the larger tag set propagates, and an aggregation downstream that was vacuously fine now carries a live obligation.

## The operation rules

For a magnitude `m` carrying tag set `T`, where each tag is nominal (equality, and cancels against itself under division):

| SQL | requires | produces |
|---|---|---|
| `m1 + m2`, `m1 - m2` | tags agree pairwise | same tags |
| `sum(m) group by G` | every tag in `T` constant within each group (present in `G`, pinned, or functionally determined by `G`) | same tags, now constant per group |
| `avg(m) group by G` | same as `sum` (algebraic: carry sum and count) | same tags |
| `m * k`, `m / k` with `k` dimensionless | nothing | same tags |
| `m1 / m2`, same type | tags agree | tags cancel to dimensionless (a Ratio or Percentage) |
| `count(m)` | nothing (values are not inspected) | a tag-free `Count` |
| `m1 < m2` as a predicate | tags agree, for the comparison to mean anything | boolean |
| `min(m)`, `max(m)`, `order by m`, top-n windows | nothing forced | a real value of the type, but its tag widens to the join of the inputs (`T` when they differ) |
| render as money at an exposure | every tag present and single-valued | leaves the typed world |

Three kinds of reduction over a tagged magnitude behave differently, and the difference is whether the operation inspects, combines, or selects values. `count` ignores values, so it is always safe regardless of tags. `sum` and `avg` combine values into a new one, so they take the hard rule above: a varying tag corrupts the magnitude (dollars added to euros are in no currency), which is why they must be discharged. `min`, `max`, ordering, and top-n selection pick an existing value rather than synthesizing one, so the magnitude they return is real; only its tag is uncertain, because the comparison that chose it was tag-blind. They therefore widen the result tag to the join of the inputs (top when the inputs disagree) rather than failing at the operation, and that widened tag is caught by the ordinary checks wherever a definite tag is later required: assignment to a typed column, a later combine, or rendering at an exposure. The same discharges that make a `sum` sound (the tag in the group key, pinned, or functionally determined) make the selection meaningful too, since they hold the tag constant across what is compared.

The additive rules (`+` needs equal tag, `/` cancels) are the single-tag fragment of units-of-measure arithmetic. Currency is a nominal tag rather than a full multiplicative dimension because `USD^2` and `USD * EUR` are meaningless: only exponents zero (dimensionless, after a ratio) and one (an amount) ever occur. The one place the full multiplicative group reappears is conversion: an `ExchangeRate` typed as `EUR/USD` multiplied by a `MoneyUSD` yields `MoneyEUR`, with the tag exponents combining as `usd^-1 * usd^1 -> 1`. So the model degrades to a tag for everyday arithmetic and recovers the group exactly where conversion needs it.

The aggregation rule is summarizability (Lenz & Shoshani, *Summarizability in OLAP and Statistical Data Bases*, SSDBM 1997; external): the validity of `sum ... group by` rests on the aggregation function being type-compatible with the measure and with the category aggregated over. Summing a magnitude across a varying tag is the type-incompatible case. The `country -> currency` discharge is reasoning about summarizability under a declared dimension dependency (Hurtado & Mendelzon, ICDT 2001; external).

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

The third path is the interesting one, because it licenses summing a tagged magnitude without carrying the tag at all. `country -> currency` means equal `country` implies equal `currency`: each country uses one currency. The groups in `sum(charge_amount) group by country` are keyed by `country`, so within any group the currency is single-valued, and the sum stays within one currency even though the `currency` column was projected away upstream. The group key recovers the constancy the dropped column would have supplied. This is summarizability under a dimension dependency (Hurtado & Mendelzon, ICDT 2001; external).

The discharge is local and the tag survives. Each group's result is a `Money` whose currency is the currency of that country, so the output is not globally single-currency; its `currency` is now functionally determined by `country`. A later `sum(total_amount)` across countries lights up again, correctly, since that aggregation has its own undischarged obligation. A functional dependency buys one sound aggregation, not blanket permission.

A functional dependency is a checkable claim, so it sits in the stronger trust class rather than being a bare assertion. The framework trusts it at analysis time to discharge the sum, and verifies it at data time by confirming the dependent tag is single-valued per key. A declared dependency that does not hold, a country that billed in two currencies after a switchover, becomes its own finding rather than silently licensing the currency-mixing it was meant to permit. Often the dependency need not be declared at all: when `currency` arrives by a join to a dimension keyed on `country`, the dependency is structural and is inferred from the join, the same way an existing `relationships` test is read as a foreign key.

## Joins: keys, grain, and tags

A join pairs rows; it does not add magnitudes. So a join carries no tag-coherence obligation of its own, and the arithmetic that follows a join reuses the rules above. What a join does change is which rows exist and how many times each magnitude appears, and that introduces a second integrity axis alongside tag coherence.

**Grain.** The grain of a relation is what one row stands for, identified by its key. A magnitude is summable only when the rows being folded are distinct at the grain that produced it, so that each underlying value is counted once. This is the disjointness condition of summarizability (Lenz & Shoshani; external) read through the key.

**Fan-out is the grain hazard.** Joining a one-row-per-order table that carries `order_total` to a many-rows-per-order line-items table replicates each `order_total` once per line item. A later `sum(order_total)` then counts each order's total several times. This is the fan trap of dimensional modeling (Kimball; external), and it is a grain violation rather than a tag violation: the currencies all agree, but the values have been duplicated. The rule: `sum(m)` is sound only when `m`'s origin key is still a key of the relation being summed. A join that does not preserve that key has fanned the magnitude out, and summing it double counts.

The four obligations a join raises, and the integrity axis each serves:

| Join concern | Obligation | Axis |
|---|---|---|
| join key types | the `ON` equality is a comparison, so its two sides' tags must agree (joining an ISO-2 `Country` to an ISO-3 one, or a `MoneyUSD` amount to a `MoneyEUR` amount, is a finding) | tag coherence |
| fan-out | a magnitude summed downstream must have its origin key preserved through every join on the path | grain |
| dependency flow | a join to a dimension keyed on `country` creates `country -> currency`; an inner join carries an existing dependency through; a fan-out can break one | discharge of later aggregations |
| outer-join misses | a `LEFT` join miss yields a NULL tag, which is an unknown currency and must block a sum until resolved | tag coherence |

Tag coherence and grain are the two preconditions for a sound `sum`: every contributing value is in one currency, and every contributing value is counted once. The currency example exercises the first; fan-out exercises the second. Both are the same underlying principle, that meaning-bearing structure must survive whenever SQL folds many rows into one, applied to two different kinds of structure.

## The lattice underneath

All of the above is one structure: a lattice of tag knowledge attached to a base type, with operations stated as require/produce over that lattice and safety decided by the lattice order. This is the type-qualifier view (Foster, Fahndrich & Aiken, *A Theory of Type Qualifiers*, PLDI 1999; external) resting on Denning's lattice model of information flow (1976; external), and it is the same meet-semilattice machinery the substrate already runs for nullability and uniqueness, which is why semantic type drops in as one more property over [lineage-facts.md](lineage-facts.md) rather than a separate engine. For currency the qualifier lattice is

```
        T   (unknown or mixed)        <- detached, or summed across currencies
       /|\
   USD EUR GBP ...                    <- a known single currency
       \|/
        _|_ (dimensionless)           <- a ratio where the currency cancelled
```

A detached amount (projected away from its currency) is the same base type at qualifier `T`; an operation is safe exactly when the operand qualifiers meet its requirement. The modern algebraic backbone for the aggregation case is semiring annotation and its semimodule extension for aggregates; the corpus carries this line through `2015_1504.04044_FAQ_Questions_Asked_Frequently.pdf`, whose mutually-commutative-aggregate condition states precisely when stacked aggregations may be interchanged, and through `2007_7aaa09842e45abf0b35eee655fea299d95b3ffa0_Update_Exchange_with_Mappings_and_Provenance.pdf`. The originating provenance-semiring paper (Green, Karvounarakis & Tannen, PODS 2007) is external to this corpus.

## What this commits the design to

- The author declares fields with their natural types and nothing else for the common case. Magnitude versus tag is inferred from field algebra, so there is no `axis` or `tag` keyword.
- Absence and presence of currency are the absent / pinned / per-row states of the `currency` field. The before-and-after of the example is the extend operation growing the tag set, which is why the obligation is retroactive and reaches undeclared models.
- The only extra declarations the author ever adds are functional dependencies to discharge an aggregation (`country -> currency`) and, for the separate semi-additive-measure problem a balance over time would raise, the dimensions a magnitude is additive over. Currency needs neither.

## Open questions

- **Inference overrides.** The magnitude/tag inference is right when the right library types are used, and the `Year`-as-`Integer` trap shows raw numeric types can mislead it. Whether the framework should warn on summing a bare `Integer` dimension, or require dimensions to be tag-typed, wants a real schema to decide.
- **Tag-blind comparison and ordering.** The working resolution above treats `min`, `max`, and ordering as value-selecting rather than value-combining: the result is a real value, its tag widens to top when the inputs disagree, and the widened tag is caught later where a definite tag is required, consistent with the naked-amount taint. This keeps the operation quiet and reuses existing machinery. The residual blind spot is the same as elsewhere, a top-tagged value that flows only into further untagged computation and never reaches a typed column, a combine, or an exposure. Whether some uses (a definitive "cheapest charge" surfaced directly to a user) deserve an eager finding rather than the lazy taint is the part left open.
- **Semi-additivity surface.** Recording that a magnitude is additive over some dimensions and not others (the balance-over-time case) is real and out of scope here. It is the one place the algebra would need an annotation the currency case does not, and it should be designed against a measure that actually needs it.
- **How far to take the multiplicative fragment.** Currency-as-nominal-tag covers arithmetic and aggregation. Full units-of-measure (rates, rate-of-rate) is reachable but only justified once conversion and derived rates appear in a real project.
