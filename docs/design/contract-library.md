# Candidate contract library

*Status*: a triage menu, not a commitment. This enumerates invariants a data
engineer commonly holds about a dbt project, so we can decide which the contract
surface should express. It is cut for the phase we are in, which is static
checking: reasoning over the substrate (facts, types, lineage) at review time,
before any data runs. Verifying a contract against data (the runtime PBT loop) is
a separate and later stream.

The vocabulary owes a lot to the tools that mapped this ground first: dbt's
built-in `unique` / `not_null` / `relationships` / `accepted_values`, the dbt-utils
and dbt-expectations test packs, Great Expectations, Soda, and elementary. Where an
entry mirrors one of those, that is a compliment to a well-chosen primitive.

## The cut for this phase

The useful question right now is not "can we run it" but "does the static
analyser have something to *do* with it today." That sorts every invariant into
one of three roles it can play now, plus a bucket the runtime loop will pick up
later:

- **[fact]** A substrate input the analyser trusts and propagates: keys, foreign
  keys, functional dependencies, grain, a declared nullability. We do not verify
  these by running; we consume them to discharge obligations along the DAG. These
  pay off the day they are written.
- **[verdict]** A claim the analyser settles itself, by propagating types and
  properties: unit and currency coherence, additivity, fan-out and grain hazards,
  a declared nullability the structure fails to preserve, a refinement that a
  transform breaks. The verdicts
  are one-sided in a way the report respects. A type-level coherence claim can be
  genuinely refuted, since two incompatible meanings admit no data at all; a claim
  about rows (fan-out and grain safety, a declared nullability) is either proven
  safe or reported as *not established*, with the defeating operator named,
  because the data may still satisfy what the structure fails to guarantee.
  `refutation-and-verdicts.md` develops the distinction. The static phase performs
  both kinds.
- **[refine]** A domain-type refinement (a range, a sign, an accepted set). It
  behaves as a **[fact]** now (it propagates, and a downstream transform that could
  violate it is a static **[verdict]**), and it also carries a data-verification
  facet the runtime loop will exercise later. We take the static half now.
- **[exec]** Only running data confirms it: an exact sum reconciliation, a
  distribution, a freshness window, a row-level identity holding across every row.
  The static phase has nothing to check here yet.

**Build now** covers **[fact]**, **[verdict]**, and the static half of
**[refine]**. **[exec]** is what we are deliberately holding.

## Holding [exec] without closing the door

We want an author to be able to write an execution-focused contract today and have
it captured and set aside, rather than rejected or silently lost. Three
disciplines keep that door open at near-zero cost:

- **Capture stays permissive.** A predicate that the static phase cannot yet
  discharge is recorded as a deferred contract ("known, not yet verified"), not a
  `ContractError`. The current capture path already keeps a malformed body as a
  finding; a well-formed but not-yet-checkable body should become a held contract
  in the same spirit. The seam is the existing fact-vs-predicate dispatch, with a
  third landing spot for "predicate the runtime loop will own."
- **The AST may represent more than the analyser consumes.** Letting a node exist
  (a string literal, a range on a value, a grouped aggregate compared to a
  constant) is cheap and non-committal. The analyser ignores shapes it does not yet
  reason over. This is how we avoid widening the AST under pressure later.
- **No capture-time invariant that presumes the runnable shape.** We keep capture
  from hardcoding "a predicate must be the two-aggregate conservation form," so the
  space stays open for the static verdicts and the runtime checks to divide the
  work by capability rather than by a frozen assumption.

## How to read the entries

Each entry is one invariant: a plain-English statement, a DSL sketch, and the role
tag above. The sketch is faithful to today's proxy surface only where it plainly
maps; elsewhere it is an *illustrative* spelling of the intent, not a proposed
syntax. Where the surface cannot express even a **[fact]** or **[verdict]** we
want, the entry notes the missing piece.

---

## 1. Uniqueness and grain

- **Single-column unique.** No duplicate values. `self.key(self.order_id)`
  **[fact]**
- **Compound key.** A tuple is unique together.
  `self.key(self.order_id, self.line_number)` **[fact]**
- **Grain / one row per entity.** Exactly one row per key.
  `self.grain(per=self.customer_id)` **[fact]**
- **Conditional uniqueness.** Unique among rows matching a predicate (at most one
  active address per customer). A predicate-scoped key; the conditional-fact
  machinery can carry it once a `where` scope exists.
  `self.where(self.is_active).key(self.customer_id)` **[fact]** (needs `where`)
- **No full-row duplicates.** No two rows identical across all columns. A key over
  every column; worth a shorthand. **[exec]** in general, **[fact]** if declared as
  a key.

## 2. Completeness and nullability

- **Not null.** A column is never null. `self.email.is_not_null()` **[fact]**
  (a nullability input the reference property propagates)
- **Conditionally required.** Null allowed only when a condition holds
  (`shipped_at` present whenever the status is shipped). A conditional nullability
  fact. **[fact]** (needs `where` and a string literal, see §5)
- **Mutually exclusive presence.** Exactly one of a set of columns is populated (a
  tagged union). `~(self.card_id.is_not_null() & self.bank_id.is_not_null())`
  **[exec]** to confirm per row; representable now.
- **Co-presence.** Two columns are null together or populated together.
  `self.lat.is_null() == self.lon.is_null()` **[exec]** (and the spelling compares
  two predicates as values today, so the shape needs work).
- **Null-rate ceiling.** At most X% of rows are null. `self.notes.null_rate() <= 0.05`
  **[exec]** (statistical).

## 3. Referential integrity and cardinality

- **Foreign key / no orphans.** Every child value appears in a parent column.
  `self.customer_id.references(models.dim_customers.customer_id)` **[fact]**
- **Nullable foreign key.** The reference holds for non-null children only.
  **[fact]** (null semantics under `references` still to settle)
- **1:1 relationship.** Each parent matches exactly one child and vice versa. A
  `references` plus a `key` on the child side; worth naming as one intent.
  **[fact]**
- **1:many with a floor.** Every parent has at least one child (every order has a
  line item). Counting parents against children. **[exec]**
- **Cardinality bound.** A parent has between N and M children.
  `self.count().group_by(self.order_id).between(1, 100)` **[exec]**

## 4. Functional dependencies and denormalization consistency

- **Functional dependency.** One column determines another.
  `self.country.determines(self.currency)` **[fact]**
- **Compound determinant.** A tuple determines a column. The AST carries a
  determinant tuple; the proxy builds it from one column today. **[fact]** (surface
  gap)
- **Denormalized attribute agrees with source.** A copied attribute matches its
  origin after a join (`orders.customer_name` equals the name in `dim_customers`
  for the same key). Declared as a functional dependency across the join now;
  confirming the copy matches the data is later. **[fact]** to declare, **[exec]**
  to verify. Needs `joined_on` on a non-aggregate comparison.
- **Consistent enum mapping.** A code and its label never disagree.
  `self.status_code.determines(self.status_label)` **[fact]**

## 5. Value domains and ranges

These are domain-type refinements: they propagate statically and a transform that
could break one is a static **[verdict]**, so we take the static half now and the
data verification rides the runtime loop.

- **Accepted values (numeric).** `self.rating.in_((1, 2, 3, 4, 5))` **[refine]**
- **Accepted values (string / enum).** `self.status.in_(("open", "shipped"))`
  **[refine]** (via `in_`; `==` on a string is a separate gap below)
- **String equality.** `self.currency == "USD"` **[refine]** (raises a raw
  `ValueError` at capture today; a string is not yet a comparison literal, and
  fixing that is a small static-side win)
- **Sign / non-negative.** `self.amount >= 0` **[refine]**
- **Inclusive range.** `self.discount_pct.between(0, 1)` **[refine]**
- **Open bounds.** `self.price > 0` **[refine]**
- **String length / format.** A regex, a length band, a known format.
  `self.email.matches(r"...")` **[refine]** to declare (surface gap), **[exec]** to
  verify.
- **Freshness / recency.** The newest row is within a window.
  `self.updated_at.max() >= now() - days(2)` **[exec]** (needs date literals, `now`,
  intervals; all execution-facing).

## 6. Cross-column arithmetic invariants

- **Additive decomposition.** A total equals the sum of its parts, per row.
  `self.total == self.net + self.tax`. The unit and currency coherence of the
  arithmetic is **[verdict]** now; the equality holding across the data is
  **[exec]**.
- **Product identity.** `self.amount == self.quantity * self.unit_price`. Same
  split: dimensional coherence now, the row-level equality later.
- **Ratio bound.** `(self.discount / self.gross).between(0, 1)`. The ratio's
  dimensionlessness is **[verdict]**; the bound holding is **[exec]**. (`between`
  on a value rather than a bare column is a surface gap.)
- **Ordering of magnitudes.** `self.min_price <= self.max_price` **[exec]** (the
  comparability of the two is a **[verdict]**).
- **Percent parts sum to whole.** Components total 100 per group.
  `self.pct.sum().group_by(self.order_id) == 100` **[exec]**.

## 7. Conservation and reconciliation across models

The conservation shape is the runtime loop's centrepiece, not the static phase's:
the analyser does not reason over an exact sum equality. Two members of this family
are exceptions the static phase already owns. A declared conservation predicate
still pays statically before the loop runs: the reader sketched in
`intent-supplying-contracts.md` walks the declared measure's lineage and flags the
hop that defeats conservation (a fan-out, a drop, an opaque seam) as
not-established findings, while the equality itself waits for execution.

- **Sum conservation across a transform.** A measure is preserved between two
  models, grouped by a shared key.
  ```python
  self.order_total.sum().group_by(self.order_id)
    == models.stg_order_items.subtotal.sum().group_by(models.stg_order_items.order_id)
  ```
  **[exec]**
- **Conservation with tolerance.** `(... == ...).within(0.01)`, absolute or
  `.relative_to(...)`. **[exec]**
- **Row-count preservation.** A transform neither drops nor invents rows.
  `self.count() == models.stg_orders.count()` **[exec]**
- **Reconciliation to an external total.** A model's aggregate matches a control
  figure in another table. **[exec]**
- **Fan-out safety.** A join does not inflate a summed measure by duplicating the
  measure side. The hazard algebra proves safety when the join is pinned to a
  covered key, and otherwise reports the join as an unproven hazard rather than a
  violation. **[verdict]**
- **Grain agreement.** A measure is summed at its own grain, not a grain the join
  multiplied. Grain propagation proves it, or reports it not established,
  statically. **[verdict]**

## 8. Temporal and sequence invariants

- **Date ordering within a row.** `self.created_at <= self.updated_at` **[exec]**
  (comparability of the two timestamps is a **[verdict]**).
- **No future dates.** `self.event_at <= now()` **[exec]** (needs `now`).
- **Effective-dated non-overlap (SCD).** Per entity, validity windows do not
  overlap. **[exec]** (a per-entity cross-row constraint).
- **Monotonic sequence.** A value never decreases over an ordered key. **[exec]**
  (windowing).
- **No gaps in a series.** Every expected period is present. **[exec]**

## 9. Set relationships between relations

- **Subset / value coverage.** Every value in one column appears in another (a
  softer `references` with no key on the parent). Declarable as a referential fact
  now. **[fact]** to declare, **[exec]** to verify.
- **Completeness of a dimension.** Every key expected downstream is present
  upstream. **[exec]**
- **No unexpected new categories.** A categorical column introduces no value
  outside a reference set (schema-drift guard). **[refine]** against a reference
  set to declare, **[exec]** to verify.
- **Disjointness.** Two relations share no keys. **[exec]**

## 10. Distribution and volume

Statistical, verified against data. Listed so we can decide whether the contract
surface reaches into this space at all, or leaves it to the freshness and
observability tools that specialize in it. All **[exec]**.

- **Row-count bound.** `self.row_count().between(1000, 5_000_000)`
- **Cardinality bound.** `self.customer_id.count_distinct().between(1, 100_000)`
- **Null-rate / duplicate-rate ceilings.**
- **Distribution stability.** A mean, stddev, or quantile within a band or within
  X% of a baseline. Anomaly detection.

## 11. Aggregation coherence (units, additivity, grain)

The static phase's home turf: whether a reduction *makes sense*, decided by
propagating domain types. These are the entries most native to where we are now.

- **Additivity.** A measure is safe to `SUM` (a count, an extensive amount) versus
  one that is not (an average, a rate, a snapshot balance). Largely a domain-type
  property; declaring it is **[fact]**, catching a `SUM` over a non-additive
  measure is **[verdict]**.
- **Shared unit / currency.** Amounts combined in an arithmetic or a sum share a
  currency or unit. Rides the `Money` type's `currency` field today; a contract form
  asserts it directly. **[fact]** to declare, **[verdict]** to catch a mix.
- **Grain agreement on join.** A measure is summed at its own grain. Overlaps with
  fan-out safety (§7). **[verdict]**

---

## Notes for triage

- The **Build now** set is the **[fact]**, **[verdict]**, and **[refine]** entries:
  the structural facts in §1, §3, §4; nullability in §2; the coherence family in
  §11; fan-out and grain in §7; and the refinements in §5. Most of these are either
  already spelled or a small surface addition.
- The single highest-leverage static-side addition is **refinements as first-class
  facts** (§5): ranges, signs, and accepted sets that propagate and can be
  defeated downstream. String and date literals are the small enabling piece,
  and fixing the string-`==` capture crash is a clean starting point.
- The **[exec]** set (all of §7's conservation members, §8, §10, and the
  data-verification facet of §5, §6, §9) is what we hold. The discipline above
  keeps those authorable and captured now, so the runtime loop inherits a populated
  space rather than a blank one.
- One scope decision to make deliberately rather than by drift: whether the
  contract surface ever reaches into the statistical space of §10, or stays with
  invariants that are proven statically or checked exactly. Deciding it now shapes
  how much of the **[exec]** door we bother to hold open.

---

## Reporting what we trust versus what we proved

Under propagate-then-track the analyser assumes each declared contract holds,
carries it as far downstream as the analysis can take it, and flags a contract only
when it can show something concrete: that a model's construction cannot honour the
declaration, that two contracts conflict, or that the construction visibly fails to
establish a declaration it was expected to carry (a declared grain the SQL provably
does not collapse to). A clean run therefore claims less than a reader might
assume, and the report has to say so plainly.

We resolve this with one standing caveat, not a per-contract status. The whole
concept a reader needs is a single sentence:

> dblect trusts the contracts you declare. It flags a contract when the analysis
> shows a model's construction cannot honour it, when two contracts conflict, or
> when the construction visibly fails to establish it. A clean run means no such
> findings, not that the contracts are proven. Checking contracts against your
> data arrives with the execution layer.

A flagged contract surfaces as an ordinary finding, and the finding's wording
carries its strength: "contradicted" where the declaration is genuinely
incompatible with what upstream declarations mean, "declared but not established,
defeated at this operator" where the data may still satisfy it
(`refutation-and-verdicts.md` draws the line). The standing caveat covers only the
remainder, the contracts the run trusted, so no per-contract status table is
introduced on either path.

Two things keep that sentence from being forgotten, without adding vocabulary a user
has to learn:

- It prints where results print, every run, not once in a preamble. The danger is a
  clean run read weeks later, so the caveat travels with the result.
- The summary counts findings, never "passed." A finding count cannot be rounded up
  to "proven" the way a green "passed" can. The report already speaks in findings
  rather than passes, which is the honest default to preserve.

This is the same discipline the report already applies to coverage. A skipped or
unbuilt model is surfaced ("no findings reported for these") so an absent finding is
not read as a clean one, and the worlds line says "no flag axes enumerated" so a
clean run is not read as covering every configuration. Trust versus proof is that
discipline on one more axis: what is surfaced here is the set of contracts we trusted
rather than proved.

### Impact on JSON and SARIF

The text, JSON, and SARIF renderings share one summary by design, so the caveat comes
from a single constant and cannot drift between them.

- **Text.** A standing caveat line beside the summary, always printed like the
  coverage block. Little added risk otherwise, since the terminal report already
  reports findings rather than passes.
- **JSON.** Machine consumers build gates ("fail the PR when findings > 0"), so the
  honest signal here is structured rather than prose. The minimal addition is a stable
  field naming the verification scope (static only, data verification pending)
  alongside the caveat text, so a consumer cannot equate an empty findings list with
  verified contracts. It stays coarse on purpose: no per-contract status, matching the
  single-caveat decision. Proven-versus-trusted counts, if we later track them, belong
  in the existing `summary` block. Adding the field bumps `JSON_SCHEMA_VERSION`.
- **SARIF.** This is the surface most able to mislead, because GitHub code scanning
  renders a green check when a run carries no error-level results, and a green check
  reads as "passed." SARIF's model is binary and has no native "trusted but unproven"
  state, so the honest lever is a single informational notification carrying the
  caveat, riding the same `toolExecutionNotifications` channel the skipped and unbuilt
  models already use. The run still reports `executionSuccessful: true`, since the
  analysis did run; the notification, not a failed invocation, carries the caveat. We
  do not emit a per-contract note result, which would reintroduce the per-row state we
  chose against and would bury the real findings under trusted-contract noise.

The load-bearing decision is that the caveat is one sentence from one source, rendered
into all three outputs through mechanisms each already has, rather than a new
per-contract status field multiplied across them.
