# Verdicts on declared contracts: what the analysis can honestly say

*Status: design proposal. The substrate distinguishes proven from unknown
everywhere, and proven-false for exactly one property. This note names the
verdicts the analysis can honestly attach to a declared contract, shows that the
common negative judgment is "not established" rather than "refuted," and derives
the one substrate change the contract-layer notes
([intent-supplying-contracts.md], [contracts-from-lineage-facts.md],
[contract-library.md]) need from it: recording the inferred value beside the
reconciled flow value.*

## The question a verdict answers

The analysis works assume-guarantee: it trusts every declared contract, carries
each as far as lineage can take it, and reasons about one model at a time under
the assumption that the upstream declarations hold. So the judgment a verdict
attaches to is not "is this contract true of the data" but an entailment:

> Do the upstream contracts, plus this model's SQL, guarantee this declaration?

That judgment has three honest answers. The structure re-derives the declaration
(proven). The structure neither re-derives nor defeats it (unknown, the
propagate-then-track default). Or the structure demonstrably fails to carry it,
with the operator that defeats it in hand. Naming these precisely, and saying
which negative the substrate can actually compute, is this note's job.

## Two negations, and which one we can compute

There are two distinct negative judgments that the one word "refuted" tends to
cover:

1. **Refuting the entailment.** The construction does not guarantee the
   declaration, and the analysis can point at the operator that defeats it: a
   join that can multiply, an outer join that pads, a union that merges two
   mappings. The data may still satisfy the declaration. This is common,
   statically derivable, and actionable.
2. **Refuting the contract.** No data satisfying the upstream contracts can
   satisfy this declaration. This is what the word "refuted" actually claims,
   and it is rare.

The counterexample that separates them: `orders LEFT JOIN regions`, with a
declared `not_null` on the joined-in `region_name` downstream. The outer-join
taint grounds a proven NULLABLE (`properties/nullability.py`,
`_outer_join_null_rule`), which conflicts with the declaration, and the
reconcile records the conflict (`lineage/property.py`, `_reconcile`, the
`provisional` taint). But if every order matches a region in production (a
foreign key that holds, whether or not it is declared), the contract is true
and the nightly test passes. NULLABLE as computed is a positive structural
claim that the join *admits* a null, not a proof that a null exists. Reporting
it as "refuted" would put a false positive against a good contract in the
flagship position; reporting it as "declared, and not established here, because
this outer join can pad it" is exactly right.

The deeper reason judgment 2 is rare: almost every contract the substrate
carries is *extensional*, a universally quantified claim about rows (no row is
null, no two rows share the key, every child row has a parent). Every
extensional claim holds vacuously on the empty relation, so a static proof of
violation needs a non-emptiness or cardinality fact, and the substrate carries
none today. Even `SELECT NULL AS x` under a declared `not_null` is only
"violated unless the relation is empty."

The exception, and the reason `domain_type` genuinely refutes today, is that
its claims are *intensional*: statements about a column's meaning, grounded
from declarations on both sides. `MoneyUSD` meeting `MoneyEUR` is a
contradiction between two meaning claims mediated by lineage, and it holds
whatever rows exist. The mutually-exclusive value shape is the mechanism that
makes the conflict computable; intensionality is the license that makes it a
refutation. Nullability shares the mutually-exclusive shape and still cannot be
refuted extensionally, which is what the counterexample above shows.

## The verdict vocabulary

Four verdicts per (scope, property, declaration), ordered by how much the
analysis established:

| Verdict | Meaning | Surfaced as |
|---|---|---|
| **Established** | the structure plus upstream contracts re-derive the declaration | nothing today; a future proven count |
| **Trusted** | neither re-derived nor defeated; the analysis trusts it forward | the standing caveat ([contract-library.md]) |
| **Not established** | a witnessed operator defeats the derivation | a finding, hazard severity |
| **Refuted** | incompatible with what upstream declarations mean; no data reconciles it | a finding, error severity |

Two boundary cases complete the picture. Declared-versus-declared conflicts at
one scope already resolve to the lattice bottom and raise `FactConflictError`
(`facts/lattice.py`, `resolve`); that is Refuted where both sides are
declarations, and whether it should soften from an error to a finding is an
open question below. And Trusted deliberately covers both "nothing spoke" and
"the analysis could not see" (an opaque UDF, an unparsed model); the
coverage machinery, not the verdict, is what tells those apart.

The rule that keeps Not established from degenerating into noise:

> **A not-established finding requires a witnessed defeater.** The analysis
> must hold the specific operator that destroyed the property, not merely fail
> to find a proof.

This matters because the property walks are conservative by design. The
uniqueness relation walk drops keys at any shape it cannot model
(`properties/uniqueness.py`, `_RelationWalk`), so "the declared key is absent
from the inferred set" is routinely the walk's own silence rather than
evidence. Firing on absence would flag every model containing an unmodeled
construct. Firing on a witness (a surviving strictly finer key, a taint whose
source node is in hand) keeps precision. Nullability already obeys this rule by
construction: its conflict arises only from a proven NULLABLE, never from
UNKNOWN, because `consistent` passes top (`facts/lattice.py`).

## What the substrate computes today, and where it goes blind

| Property | conflict detected? | conflict recorded? | conflict surfaced? |
|---|---|---|---|
| `domain_type` | yes (`consistent`) | yes (`provisional`) | yes (`DOMAIN_TYPE_CONTRADICTION`, `check/run.py`) |
| `nullability` | yes (`consistent`) | yes (`provisional`) | no |
| `uniqueness` | **never consulted** | no | no |
| `functional_dependency` | **never consulted** | no | no |
| `referential` | no property yet | — | — |

The nullability row is the near-free win: the conflict is already computed, and
what surfacing it yields is a not-established finding, never a refutation.

The uniqueness and FD rows are blind for a mechanical reason. Both reconcile by meet (`reconcile_by_meet`,
`lineage/property.py`): declared and inferred keys are same-polarity lower
bounds, so the flow value is their union and `consistent` is never consulted.
The consequence bites the contract layer directly: after reconcile, the stored
annotation *always contains the declared key*. A declaration checks itself and
passes. The grain-drift emitter (#202) has nothing in the store to compare
against, because the value it needs, what the SQL derived *before* the
declaration was unioned in, is computed inside `propagate` and discarded.

## The design: record the inferred value beside the flow value

The substrate change this note proposes is small: `_reconcile` already holds
the grounded and inferred annotations in hand; record the pre-reconcile
inferred annotation alongside the flow value instead of discarding it. The flow
value keeps its exact meaning (trust the declaration forward, the
assume-guarantee posture is untouched); the inferred value becomes readable
where today it is not.

Everything else in this note is a *reader* of that pair:

- **Verdicts are derived, not stored.** Established: `consistent(declared,
  inferred)` holds with the inferred value concrete, or for meet-reconciled
  properties the inferred value refines the declaration. Not established: the
  pair conflicts and a defeater is witnessed. Trusted: everything else.
  `consistent` already generalizes across every property, including the
  meet-reconciled ones once the pre-reconcile value exists to feed it:
  `consistent(declared_keys, inferred_keys)` fails exactly when the derivation
  did not re-derive the declared keys.
- **Conflict sites come for free.** The `provisional` bit is one OR-propagated
  flag with no provenance, so taint-reach reporting (what
  `_contradiction_findings` does for the rare domain-type conflict) would be
  noisy for nullability, where outer joins are everywhere. With the pair
  recorded, the conflict *site* is simply the scope where a concrete grounding
  fails `consistent` against its own inferred value; downstream scopes that
  merely inherit the taint need not re-fire.
- **No new lattice theory.** No negative lattices, no De Morgan duals, no
  bilattice reducer. The authoring surface for a property is unchanged.

### What each property's reader yields

**Nullability.** The finding: "declared NON_NULL is not established here; this
outer join (or NULLIF, or NULL literal) admits a null." The defeater is already
witnessed by the taint machinery, and `outer_join_nullable_columns` already
surfaces which join padded which column for the join-on-nullable-key finding,
so the render reuses it. Severity is hazard grade. One source deserves a
stronger word later: a bare NULL literal under a declared NON_NULL is violated
on every existing row, a fact worth distinguishing once findings carry an
evidence grade, but it still is not a refutation while the relation can be
empty.

**Uniqueness (the #202 grain emitter).** The finding: "declared grain
`order_id` is not established; the construction carries `(order_id,
line_number)` through un-collapsed." The witness rule is doing real work here.
A per-line key does not contradict the declared grain (every order may have
exactly one line), and its mere absence from the inferred set may be the walk's
conservatism, so the emitter fires only when a strictly finer key survives to
the output with no collapse to the declared grain, and coverage runs through
the FD closure (`determines`), exactly as `detect_join_fanout`'s hardening
does, so a non-minimal grain does not false-fire.

**Functional dependency.** Same reader, same witness rule: a union whose arms
each carry the declared `zip -> city` and whose merge drops it is a witnessed
non-preservation (each arm can honour the mapping while the two disagree with
each other). Proving an actual violation would take value-level reasoning about
the arms' contents, which is the runtime loop's territory.

**Domain type.** Folds into the shared reader as its already-surfaced special
case, keeping its "contradiction" wording and error severity, which its
intensional license justifies.

**Referential.** Waits on a property existing at all; the declaration edges in
`types/bridge.py` have no lattice to reconcile.

## Proven negatives: deferred, and why

The natural companion design is a per-property negative-evidence channel: a
negative lattice beside each positive one (a proven duplicate, a proven FD
counterexample), the De Morgan dual of the positive lattice, with a verdict
reading both. Applying this codebase's own discipline, discharging each
proposed grounding to an exact decision procedure, empties it:

- A CROSS join proves a duplicate only if the other side provably has at least
  two rows.
- A `UNION ALL` of overlapping arms proves one only if some value provably
  appears in both arms, and an arm is provably non-empty.
- A "known-duplicate source" requires someone to declare a negative, which no
  surface offers.

Every proven negative for an extensional property is conditional on a
cardinality or non-emptiness fact, and the substrate grounds none (row-count
intervals are open as #38). Dual lattices would land with an empty grounding
set.

What the enumeration actually produces is *conditional* negatives: "a duplicate
exists unless that side is empty," "a null lands unless every row matches."
That shape already has a home. The hazard algebra
([hazard-algebra.md], the effect, consumer, and guard layers) is precisely the
calculus of "this operator introduces the effect, that consumer is sensitive to
it, this guard discharges it." A negative-evidence channel would re-derive the
same structure with stronger and mostly unreachable grounding. If a stronger
grade is ever wanted, the cheaper shape is an **evidence grade on the hazard**
(possible, versus proven given non-emptiness) once cardinality facts exist,
rather than a parallel lattice per property. The De Morgan symmetry (fan-out
destroys a key and creates a duplicate; DISTINCT restores the key and destroys
the duplicate) remains a good soundness cross-check for that future work, with
one warning: `detect_join_fanout` fires on unproven coverage, the Unknown case,
and must never be relabeled a proven negative.

## Reporting

Findings are the only per-contract surface, which keeps this note compatible
with the single-standing-caveat decision in [contract-library.md]: a flagged
declaration surfaces as an ordinary finding whose wording carries its grade
("contradicted" for Refuted, "declared but not established, defeated at X" for
the witnessed hazards), and the contracts the run merely trusted stay under the
one caveat, with no per-contract status table in text, JSON, or SARIF. The
verdict vocabulary is how the analysis and the docs *reason*; it is not a new
reporting axis. The runtime loop is the other consumer: Trusted and
Not-established declarations are its natural priority queue, and a witnessed
defeater tells it exactly which hop to test.

## Sequencing

- **The contract-layer notes are not blocked.** Their emitters are
  not-established emitters, and nothing they propose consumes machinery that
  does not exist.
- **The two-channel record lands with its first consumer**, the #202 grain
  emitter, since that is the reader the record exists for.
- **The nullability not-established finding** follows, reusing the shared
  reader with site-localized reporting.
- **The FD reader** reuses the uniqueness machinery.
- **Proven negatives wait** on cardinality and non-emptiness facts (#38 and
  relatives) and re-enter, when they do, as an evidence grade on hazards.
- **Referential** waits on the property.

## Open questions

- Should `FactConflictError` (declared versus declared, mutually unsatisfiable)
  soften from a run-stopping error to a Refuted finding, now that a finding
  grade exists for it to land in?
- Where does the witnessed defeater live structurally? The nullability taint
  carries it in the graph; the uniqueness walk would need to return the
  surviving finer key alongside the key set, a small widening of the reducer's
  return channel.
- Does Established deserve surfacing (a proven count beside the caveat), or
  does counting proofs invite the same false comfort the finding-count
  discipline avoids?

## A note on the theory

Belnap's four-valued bilattice (Belnap 1977) is the natural first reach for
declared-versus-inferred reasoning, and its knowledge-versus-truth factoring is
what exposes the gap this note is about. The reason it does not transplant
directly is that its False fuses the two negations separated above. The
judgment the analysis makes is
a sequent, "upstream contracts entail the declaration through this model," and
the verdicts here are the classic proved / unknown / disproved answers of
static verification applied to that sequent, under the assume-guarantee
tradition (Misra and Chandy 1981; Jones 1983) the contract library already
leans on. Disproving the sequent is common and cheap; disproving the
declaration itself needs the intensional license that `domain_type` has and the
extensional properties lack. The K-relations framing under the uniqueness
lattice (Green, Karvounarakis, Tannen 2007) sits comfortably beside all of
this, unchanged.

[intent-supplying-contracts.md]: intent-supplying-contracts.md
[contracts-from-lineage-facts.md]: contracts-from-lineage-facts.md
[contract-library.md]: contract-library.md
[hazard-algebra.md]: hazard-algebra.md
