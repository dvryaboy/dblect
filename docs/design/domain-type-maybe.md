# Domain types: a may/must refinement for widened magnitudes

*Status: design note, follow-up. This sketches a completeness refinement to the
domain-type algebra in [domain-type-algebra.md](domain-type-algebra.md). It is not a
soundness fix: the algebra is already sound without it. The aim is to recover findings
the current lattice conservatively drops. Citations are to the primary literature.*

## The gap

The domain-type lattice has one no-claim top, `NAKED`. Today it carries two situations
that behave differently:

- **A bare scalar or an untagged column.** We genuinely know nothing about its unit. A
  numeric literal is handled separately as polymorphic; an untagged column is simply
  unknown.
- **A widened sum.** `usd_amount + untagged` widens to `NAKED` because the addend's unit
  is unknown, so the sum carries no clean dimensional claim. Yet we know *by
  construction* that this value involves dollars: a usd quantity was summed into it.

Collapsing the second into `NAKED` discards recoverable information. The cost is a false
negative: `(usd + untagged) + eur` widens the inner sum to `NAKED`, so the outer addition
sees no disagreement and the dollars-plus-euros mix goes unflagged, even though we could
have known a usd quantity was being added to a eur one.

This is a completeness gap. The analyzer never makes an unsound claim here; it makes no
claim where it could have made a useful one.

## A may/must split

The standard remedy is a may/must distinction, the same kind of split abstract
interpretation draws between facts that hold on every path and facts that hold on some
(Cousot & Cousot, *Abstract Interpretation*, POPL 1977). Three states instead of two:

- **MUST-δ** (today's clean tag, e.g. `usd`): the value transforms exactly as the
  monomial δ. Certified. This is what discharges a `sum` and what the rescaling oracle in
  the soundness PBT verifies.
- **MAYBE-δ** (new): δ is implicated but not certified. Precisely: *this value is δ if it
  is well formed at all*. It arises when a known unit is combined with an unknown one
  under `+`, so the value either is δ (if the unknown turned out compatible) or is already
  ill formed (if it did not). It is never a clean δ.
- **NAKED** (top): no unit implicated at all. A pure scalar, or a column we know nothing
  about.

`MAYBE-usd` is, equivalently, an *additive taint*: the set of clean units that have been
added into the value. A set of size one with an impurity is `MAYBE` that unit; a set of
size greater than one is already a `CONFLICT`; a clean single unit with no impurity is
`MUST`.

## What it buys, and what it does not

The payoff is the recovered conflict, and it is sound to report:

```
(usd + untagged)        -> MAYBE-usd        (instead of NAKED)
MAYBE-usd + eur         -> CONFLICT          recovered
```

`MAYBE-usd + eur` is a genuine finding because no value of the unknown rescues it: if the
unknown was usd the inner sum was usd and the outer adds eur to usd; if it was anything
else the inner sum was already ill formed. Either way the dollars-and-euros mix is real.

What it does not change is soundness. A `MAYBE-δ` never asserts a clean dimension, so it
cannot discharge a `sum` and the rescaling oracle has nothing to falsify in it. The
soundness work (no-claim absorbing under the operators, polymorphic literals) stands on
its own and does not need this.

## Two rules that keep it honest

1. **Addition is where it pays; multiplication is where it gets murky.** Under `+` and
   comparison, `MAYBE-δ` is well behaved: it conflicts on a second unit and otherwise
   carries δ forward conditionally. Under `*` and `/`, "an unknown factor" is not
   "maybe-δ", it is `δ · α` for an unknown unit variable α, which is the full unit
   polymorphism of Kennedy (*Dimension Types*, ESOP 1994). Rather than open that, scope
   the refinement to `+` and comparison and let `*`/`/` keep treating an unknown operand
   as absorbing.

2. **This is verification, not inference.** The tempting move is to treat the unknown as a
   solvable variable, unify `usd = α`, and conclude a clean `usd`. That is exactly the
   over-claim the soundness PBT rejects. `MAYBE-usd` means *must be usd if anything*, a
   conditional claim used only to detect conflicts. It must never collapse into *assume
   usd and solve*. The line between the two is the line between a verifier and a type
   inferencer.

## How it would sit in the code

`MAYBE-δ` is a value-lattice concept: it would participate in `meet`, `join`, and the
transfer rules with its own monomial, alongside the existing `Tagged` and the conflict
bottom. It is distinct from the `provisional` bit on an `Annotation`, which is an
error-recovery taint that "never licenses a more precise value"; `MAYBE` is the opposite
direction, a value carrying *more* than `NAKED` for the purpose of catching a conflict.

## Validating it needs a different oracle

The empirical-soundness PBT rescales declared units and checks that a claimed clean
dimension predicts the result. It is blind to this refinement by construction: a `MAYBE`
output asserts no clean dimension, so rescaling has nothing to falsify, and the payoff is
catching conflicts, which is a false-negative axis the rescaling law does not touch. So
the contract here needs a conflict-completeness oracle: a generator of expressions that
*must* be flagged (a value with a usd additive contribution, later added to or compared
with a different unit), asserting the analyzer reports each. That contract should be
pinned before the lattice element is built.

## References

- Cousot, P., & Cousot, R. (1977). Abstract Interpretation: A Unified Lattice Model for
  Static Analysis of Programs by Construction or Approximation of Fixpoints. *POPL*.
- Kennedy, A. J. (1994). Dimension Types. *ESOP*.
