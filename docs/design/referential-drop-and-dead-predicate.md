# Referential orphan drop and dead predicate: implementation plans for §3 and §6

*Status: implementation plan, not yet built. `intent-supplying-contracts.md`
proposes seven contract shapes; §4 (grain) and §5 (functional dependency) are
already built and are in live, uncommitted use against tuva-core. §3
(referential integrity) and §6 (dead predicate) are not, and the doc's own
estimate of how much wiring each still needs turns out to be optimistic for
§6 in particular. This note is the result of an audit of the current source
tree plus two worked plans, one per section. File and line references below
are current as of 2026-09-20 (`HEAD` at `8ebf61e`); re-verify them against
whatever `HEAD` is by the time you start, since the tree moves and a plan is
not a patch.*

## Where this sits

`intent-supplying-contracts.md` (branch `docs/intent-supplying-contracts`,
not yet merged) is the parent document; its §3 and §6 are reproduced in
enough detail below that this note stands on its own. Two sections of that
doc are relevant background rather than part of what to build: §1
(conservation) shares its "walk the lineage from a declared origin to a
point of use, classifying each hop" framing with part of §3, and
`refutation-and-verdicts.md` defines the verdict vocabulary (proven,
not-established, trusted, contradicted) both plans below have to slot into.

## §3: referential integrity over a nullable or non-unique key

### The shape

A join that discards rows with no match on the other side is ordinary SQL,
correct almost everywhere, so flagging it undeclared is noise. Declare that a
column is a foreign key, though, and an inner join across it stops being
ambiguous: every child is expected to match a parent, so a silent drop is now
something the analysis can honestly report. The doc's example:

```sql
select o.order_id, o.amount, r.region_name
from {{ ref('stg_orders') }} o
join {{ ref('dim_regions') }} r on r.region_id = o.region_id
```

A nullable `o.region_id` and a non-unique `dim_regions.region_id` already
fire today (`detect_join_on_nullable_key`, `detect_join_fanout`). The orphan
drop, a well-formed non-null key that simply has no parent row, does not,
and cannot without a declaration.

### What's already built

The substrate for this is complete; only the check itself is missing.
`ColumnProxy.references()` (`src/dblect/contracts/proxy.py`) builds a
`ReferencesFact`, which `types/bridge.py:_lower_references` resolves into a
`ForeignKeyEdge(child: ColumnRef, parent: ColumnRef)` (`bridge.py:95`, no
provenance or detail field yet). `foreign_key_edges()` (`bridge.py:678`)
merges contract-declared edges with `dbt_relationship_edges()`
(`bridge.py:616`), de-duplicated. Its own docstring says: "the merge point a
future fan-out finding or fixture generator reads from." Nothing calls it.

One gap worth fixing alongside this: `dbt_relationship_edges` reads a
`relationships` test's `column_name`/`field`/`to` kwargs but never inspects
`tm.where`, so a `where`-scoped relationships test (a conditional claim) is
currently read as an unconditional edge. That should be decided explicitly
rather than silently overreaching.

### Architecture: a declaration-graded reader, not a grain-shaped emitter

Build it as a new module, `src/dblect/check/referential.py`, shaped like
`check/run.py:_join_key_findings`: a direct structural read over the
resolved, `ColumnRef`-stamped tree, gated on a declaration. Not shaped like
`check/grain.py`.

`grain.py` compares a *pre-reconciliation* derived value against a
declaration at the declaring model: what the SQL alone establishes, before
the declaration merges in. An orphan drop has no derived value to compare.
The SQL neither re-derives nor defeats the foreign key; it just contains a
join whose shape determines whether an unmatched child survives. There is
also no need for a `ResolvedPredicate` reader first (the machinery §1
calls for): that reader is for a value that flows and gets apportioned
across a lineage path, and the orphan drop needs no such lattice, only a
join site and a declared edge. Building the shared reader can come later,
as a generalization once conservation checks want it too, not as a
prerequisite for this section.

The builder already stamps every column reference, not only projected
ones, with a resolved `ColumnRef`: `lineage/builder.py`'s `_Walker` writes a
resolved ref back for a reference "in any position (an UNNEST argument, a
join key, a filter)," resolving through CTE and derived-table scopes. That
is what lets a join's `ON` clause resolve straight to the same `ColumnRef`
vocabulary a declared `ForeignKeyEdge` uses, with no separate name index and
no special handling for a join buried inside a CTE body (which is exactly
the tuva-core shape below).

### A decision procedure closed over every join kind

The existing outer-join helpers in `src/dblect/sql/_sqlglot.py`
(`outer_join_optional_aliases`, `joins_with_outer_dropped_aliases`) only
answer "which side is nullable," which conflates two different questions:
which aliases get NULL-padded, and which aliases lose unmatched rows
entirely. CLAUDE.md's soundness rule (a check that branches on a closed
type decides every case explicitly) argues for replacing both with one
function that answers both questions over every `JoinSide`:

```python
@dataclass(frozen=True)
class JoinRowEffect:
    join: exp.Join
    side: JoinSide
    optional: frozenset[str]           # aliases NULL-padded on unmatched rows
    dropped_unmatched: frozenset[str]  # aliases whose unmatched rows leave the result

def join_row_effects(sel: exp.Select) -> list[JoinRowEffect]: ...
```

decided by one `match side:` closed by `assert_never`:

| side  | optional                    | dropped_unmatched            |
|-------|------------------------------|-------------------------------|
| INNER | ∅                            | right ∪ accumulated_left      |
| LEFT  | {right}                       | {right}                       |
| RIGHT | accumulated_left              | accumulated_left              |
| FULL  | right ∪ accumulated_left      | ∅                              |
| CROSS | ∅                             | ∅ (no match predicate, nothing is "unmatched") |
| SEMI  | ∅                             | accumulated_left (a probe row without a match is dropped; the right side is never in the output) |
| ANTI  | ∅                             | ∅ (unmatched rows are exactly what it keeps; matched rows are dropped, which is not an orphan drop) |

The two existing functions become projections of this table (behavior
preserving; their existing tests are the regression net). This also
discharges CLAUDE.md's instruction to re-establish an existing fact's
soundness across its full input space when a new consumer arrives, since
today's `joins_with_outer_dropped_aliases` returns the empty set for INNER
by fiat rather than by reasoning through the case.

### Files, functions, types

- **`types/bridge.py`**: give `ForeignKeyEdge` a `provenance: Declared` and a
  `detail: str | None`, so a finding can say which contract field or which
  dbt test grounds the edge, and so the dedup in `foreign_key_edges` can
  prefer the contract edge's detail when both exist. Decide the `tm.where`
  case in `dbt_relationship_edges` explicitly (skip a conditional test, or
  carry the condition; either is fine, silence is not). Add
  `relationship_tested_edges(manifest)` (or a filter over provenance) as the
  guard set the check reads (see below).
- **`sql/_sqlglot.py`**: `JoinRowEffect`, `join_row_effects`, with
  `outer_join_optional_aliases` and `joins_with_outer_dropped_aliases`
  reimplemented over it.
- **`check/referential.py`** (new): `OrphanDropSite(join, side, child,
  parent, edge)`; `orphan_drop_sites(tree, edges_by_child, ref_of)`, the
  pure signal, deciding per `SELECT` and per `JoinRowEffect`:
  1. No `ON` clause (CROSS, NATURAL, bare `USING`) skips. `USING (k)` is a
     real shape worth a documented miss in the first cut rather than a
     guess.
  2. A join already recognized as the `LEFT JOIN ... IS NULL` anti-join
     idiom (`anti_join.py`) skips; that idiom keeps orphans on purpose.
  3. For each conjunctive equality leaf between two columns, resolve both
     to `ColumnRef`s and check whether `{left, right}` matches
     `{edge.child, edge.parent}` for some declared edge. Other conjuncts
     (a literal pin, a second key column) only narrow matches, so the drop
     they might cause is a superset of the one being checked; they're
     read for narrowing, not required.
  4. Fire only if the child's alias is in `effect.dropped_unmatched`.
  5. Fire only if the edge isn't already covered by an enabled,
     unconditional `relationships` test (see the verdict discussion below).
  6. Nullability is out of scope here; `detect_join_on_nullable_key` owns
     the NULL-key drop, this finding is about a non-null key with no
     parent. Both can fire on the same join.
- **`check/findings.py`**: a `REFERENTIAL_ORPHAN_DROP` kind.
  **`severity.py`**: `WARN` (a new `CheckFindingKind` is a type error under
  `severity.py`'s closed `match` until it has one, and both
  `tests/test_fail_threshold.py` and `tests/audit/test_suppress.py`
  parametrize over `list(CheckFindingKind)`, so the enumeration suites pick
  up the new kind with no test edits).
- **`check/run.py`**: thread the merged edges and the guard set through
  `CheckGraphs`, call the new reader alongside `_join_key_findings`.

Suggested wording, never "violated": *"this JOIN discards rows of
`stg_orders` whose non-null `region_id` has no match in `dim_regions`.
`region_id` is declared a foreign key to `dim_regions.region_id`, so no such
rows are expected; if that ever stops holding, they vanish here with
nothing reporting it. Guard the edge with a `relationships` test so a
violation fails loudly, or switch to a LEFT JOIN and handle the unmatched
side explicitly."*

### Verdict grade: WARN, and why

The proposition under test is not the foreign key itself. Under the
analysis's assume-guarantee posture the FK is trusted forward, and this
join can neither establish nor refute it, the same reasoning PR #238 used
to redesign the declared-FD self-check as a world axiom rather than a
"not established" comparison (a declared `determines` is a fact about the
declaring model's world, not something SQL structurally proves or
disproves). A declared foreign key is the same kind of axiom.

What the join changes is not the FK's truth but its failure mode: the
proposition "every child row survives this model" holds exactly when the
FK holds in the data, and the inner join is the operator that turns a
violation into a silent drop instead of a loud one. That's a hazard
grade, `WARN`, never `ERROR`, because the data may satisfy the FK
everywhere the whole life of the pipeline, in which case the inner join is
simply correct code. The wording above avoids ever calling the FK
"not established," unlike the grain check, where the SQL really can carry
positive evidence against the declaration (a strictly finer key surviving
to the output). Here the SQL carries no such evidence either way.

The recommended guard, firing only when no enabled unconditional
`relationships` test already covers the edge, exists because otherwise
every inner join across every tested FK in an ordinary star schema fires,
which is exactly the noise §3 exists to avoid. A tested edge already has a
loud failure mode (the test fails), so the entire content of this finding,
that the failure mode is currently silent, is already false for it.

### Two tuva-core fixtures that pin the line this check has to draw

`encounters__stg_medical_claim.sql` inner-joins every claim line onto both
`encounters__patient_data_source_id` (`on m.person_id = d.person_id`) and
`service_category__service_category_grouper` (`on m.claim_id = g.claim_id`).
These read almost identically in SQL, and the check needs to treat them
oppositely:

- `encounters__patient_data_source_id` is `select distinct person_id,
  data_source from normalized__medical_claim union ... eligibility`, so the
  edge from a claim's `person_id` into it is total by construction (only a
  NULL `person_id` fails, which is the nullable-key path this check
  doesn't own). Once `person_id` carries a declared or dbt-sourced
  `ForeignKey`, this join is the genuine positive fixture.
- `service_category__service_category_grouper` documents itself as
  excluding invalid or unrecognized `claim_type` rows on purpose. A
  declared *total* FK there would be a false declaration, not a missing
  one, which is the doc's own point made concrete: an inner join is
  "indistinguishable from an intended filter" absent a declaration, and
  here the filter really is intended. This is the fixture that must stay
  silent, both because nothing is declared today and because declaring a
  total FK against it would be wrong.

### Test-first artifacts

Write these before any implementation:

1. `tests/sql/test_join_row_effects.py`: the closed table above, every
   `JoinSide` crossed with the child on the accumulated-left side versus
   freshly joined-in, plus a multi-join chain pinning left-side
   accumulation, plus `USING`/`NATURAL`/no-`ON` rows, plus the two existing
   projections re-asserted unchanged on their current fixtures.
2. `tests/check/test_orphan_drop.py`, through the existing `run_check`
   harness: the join-kind × child-position table with expected fire/silent
   and a wording fragment; the `LEFT JOIN ... WHERE p.k IS NULL` idiom
   silent, a non-key `WHERE p.attr IS NULL` not-the-idiom following the
   plain LEFT row; a composite or literal-pinned `ON` firing; an equality
   under `OR` silent; `USING` silent (documented miss); the join inside a
   CTE body whose FROM is the child model (the tuva shape) firing; no edge
   declared silent (the tuva `service_category` shape); a
   relationships-test-only edge silent, contract-only firing,
   `where`-scoped test firing (not a guard), disabled test firing; the
   NULLABLE-key case producing both this finding and
   `JOIN_ON_NULLABLE_KEY` together; message pins including
   `"violat" not in message.lower()`.
3. A data-as-judge property test reusing the existing DuckDB oracle
   fixture: generate a parent table, a child table with at least one
   orphaned key, and a join drawn from a grammar over every `JoinSide` and
   child position, and assert the check fires if and only if the
   materialized output is actually missing an orphaned child row. This is
   the exact decision procedure for "does this join discard unmatched
   children," with the warehouse as the oracle, and it has to fail on any
   of the seven `JoinSide` arms being mis-decided.
4. Once the suite is green on its first run (itself a smell per CLAUDE.md),
   inject deliberate breaks and confirm each fails a named test: SEMI
   treated like ANTI, INNER dropping only its right side, the guard gate
   inverted, an `OR`-leaf treated as a conjunct.

### Sequencing and open questions

Suggested order: the `ForeignKeyEdge` provenance and `where`-handling fix,
then the `join_row_effects` generalization, then the check and its wiring.
A natural follow-up, not a prerequisite, is a carrier property so a
declared edge survives a rename-only passthrough model rather than only
matching an exact `ColumnRef`, and a further one is threading the edges
into the existing undeclared detectors so a fan-out or nullable-key finding
can name the relationship it's threatening ("the doc's own "sharpens their
framing" point for §3).

Open for whoever implements this:

- Silence only when a `relationships` test already covers the edge
  (recommended), or fire always at `WARN` and simply name the covering
  test if one exists? The second needs a second, quieter kind or an
  `INFO` grade, since severity is assigned per kind.
- A `LEFT JOIN ... WHERE p.col = x` (a non-null-check predicate that turns
  a LEFT join into an effective INNER): should this check reclassify it as
  INNER for the purpose of the edge, given `where_provenance` already
  handles a related shape for nullability?
- Whether to resolve `USING (k)` in the first cut or document it as a miss;
  it needs the shared column's stamp to exist, which may not be guaranteed
  today.
- Composite keys: both `ForeignKey` and dbt `relationships` are
  single-column, so a composite `ON` clause is judged per single-column
  edge; worth saying so explicitly in the message.

## §6: dead predicate over a closed value set

### The shape

```sql
select order_id, status
from {{ ref('stg_orders') }}
where status = 'shipd'   -- the real value is 'shipped'; this filter is always empty
```

Nothing about this SQL is invalid; it just returns zero rows forever. The
same shape is a `CASE` whose arms miss a member, silently routing it to the
default. A `NominalEnum` names the closed set a column's value is drawn
from, and once bound, a literal outside it is provably dead.

### Why this needs more than the doc estimates

`intent-supplying-contracts.md` calls this "small, and it rides the AST the
proxy already builds and the set #135 or #36 carries." Both of those are
still open issues, not shipped facts to ride on:

- **#135** ("propagate standalone closed-category columns") already
  diagnoses the exact gap: a bare `status: OrderStatus` annotation *is*
  classified as `FieldKind.NOMINAL` by `classify()` in
  `types/contract.py:_build_declaration`, but `bridge.py:_resolve_one`
  routes it to `ScalarDecl`, which today "carries no fact... in this
  build." Recognized, then inert.
- **#36** ("accepted-values and range discoverers") is the zero-declaration
  companion, grounding a set from a dbt `accepted_values` test or a native
  `CHECK ... IN`. Also unstarted; the only trace in the tree is a
  provenance-enum comment naming `accepted_values` as a known dbt test
  kind, not a discoverer that reads one.

So there is, as of today, no lineage fact anywhere for a "read the set at a
`Compare`/`InSet`/`CASE`" check to read, no matter how the set gets
declared. The right scope for this section is a small, new `ValueDomain`
lineage property that closes both issues in one pass rather than either
half alone.

`NominalEnum`/`UnitEnum` today (`types/enums.py`) are plain `enum.StrEnum`
marker bases, used only as a facet riding *inside* a `DomainType` (tuva-core's
`ClaimAmountBasis(UnitEnum)` on `ClaimAmount`, for instance), never bound
directly to a bare column. #135's own fix belongs on this same property
rather than as a `DomainTag` extension: a `DomainTag`'s nominal facet
carries an identity, not a member set, and the dead-predicate check needs
the members.

### Files, functions, types

- **`lineage/properties/value_domain.py`** (new; `domain_type.py` is the
  template, and the home of the `join_key_conflicts`-shaped pure-signal
  pattern both this and §3's reader follow): `ValueDomain = Unbounded |
  Bounded(values: frozenset[Lit])`, reusing the existing `Lit` type from
  `lineage/predicate.py` so `1` and `'1'` stay distinct. Meet is
  intersection, join is union, `Unbounded` is top. Every transfer rule is
  named and closed rather than inferred by a generic fold: `Column`/`Alias`
  pass through; a literal grounds a singleton; `NULL` is top (a value
  domain describes non-null values, so `Bounded(∅)` would be a false
  contradiction); `COALESCE` unions its children; `CASE` unions every THEN
  branch and the default (needs its own rule rather than the generic child
  fold, which would incorrectly union in the WHEN conditions too);
  `CAST` widens to top; and a catch-all `exp.Expression -> top` so every
  unlisted node (string functions, arithmetic, windows) widens rather than
  folds. That catch-all is what keeps the property sound by default, and
  the property test below should treat it as the single highest-value row
  to pin.
- **`lineage/facts/model.py`** or a sibling discoverer module:
  `accepted_values_discoverer()`, reading an enabled `accepted_values` test
  the same way the existing `unique`/`not_null` discoverers read theirs,
  producing a `Fact[ValueDomain, ColumnRef]` with `Declared(DBT_GENERIC_TEST)`
  provenance. Decide explicitly (not silently) what happens for a
  `where`-scoped test and for a non-string/non-numeric value list.
- **`types/bridge.py`**: in `_resolve_one`, a `ScalarDecl` whose field binds
  a `NominalEnum` (or a `DomainDecl` whose bound enum facet isn't fixed)
  grounds a `ValueDomain` fact on that column, closing #135's propagation
  gap and removing the "`ScalarDecl` carries no fact" comment.
- **`check/dead_predicate.py`** (new): reads the propagated `ValueDomain`
  at each stamped column reference and applies the decision tables below.
  Two kinds: `DEAD_PREDICATE` and `CASE_LEAVES_ENUM_MEMBER_UNHANDLED`.
- **`check/run.py` / `findings.py` / `severity.py`**: propagate the new
  property over the column graph, wire the reader in, add the two kinds
  and their severities.

### The decision tables

*Comparison form*: `EQ`/`NullSafeEQ` is dead when the literal isn't a
member; `NEQ`/`NullSafeNEQ` is constant-true under the same condition;
`IN (...)` reports each stray literal individually, "always false" only
if every one is stray; `NOT IN` treats a stray literal as a no-op;
ordering comparisons (`<`, `<=`, `>`, `>=`, `BETWEEN`), `LIKE`-family, and
`IS` are silent, since a nominal set carries equality only; a
column-to-column equality is silent in this first cut (a natural
follow-up: meet the two columns' sets at a join key, the same shape
`_join_key_findings` already reads for domain types).

*Literal class*: an exact member is silent; a non-member of the same kind
fires; a string that differs from a member only by case fires with its own
wording rather than the plain dead-predicate one; a kind mismatch (a
numeric literal against a string-valued set) is a case to decide
explicitly rather than default into either silence or a fire; a literal
`NULL` operand is silent.

*Boolean context*, found by walking up from the comparison to its nearest
owner: a top-level `WHERE`/`HAVING`/`QUALIFY` conjunct reads "the result is
always empty" (or "never filters anything" for the constant-true case); a
`JOIN ... ON` conjunct reads "never matches"; under `OR` it's "this
disjunct never matches"; under `NOT` the polarity flips; inside a `CASE
WHEN` it's "this arm is never taken"; as a bare projected scalar it's
"always false."

*CASE coverage*: only decidable when every arm compares one bounded column
against a literal (a simple `CASE col WHEN ...` or a searched `CASE` whose
every arm is `col = lit`/`col IN (...)`); any arm of another shape makes
coverage unknown for the whole CASE, though a dead individual arm still
fires on its own. Fire the coverage finding only when the CASE has no
`ELSE`, or an explicit `ELSE NULL`; an explicit non-null `ELSE` (the
`sum(case when status = 'cancelled' then 1 else 0 end)` idiom) is silent,
because that shape is a deliberate complement, not an oversight, and
firing on it would be the exact test-theater CLAUDE.md warns against.

### Verdict grade: ERROR for the dead predicate, WARN for CASE coverage

`DEAD_PREDICATE` gets the stronger grade in this pair of plans. The
literal's singleton meets the column's declared or grounded set to the
lattice bottom at the comparison itself: a genuine contradiction between
two meaning claims (the enum's intensional claim about what the column
means, and the literal's own claim), true regardless of what data exists.
That's the same license `DOMAIN_TYPE_CONTRADICTION` already uses, so:
**`ERROR`**. Two honesty caveats worth stating in the finding's own
wording: the claim is exact relative to the declared or discovered set, so
a stale declaration produces a finding that is right about the
declaration and wrong about the data (in which case the accompanying
`accepted_values` test, if any, would also be failing); and the whole
approach is sound only because every unmodeled operator widens to top
rather than narrows, which is exactly what the property test's catch-all
row has to pin.

`CASE_LEAVES_ENUM_MEMBER_UNHANDLED` is decidable but intent-dependent (an
`ELSE`-less mapping may be entirely deliberate), so it gets the lighter
grade, **`WARN`**, on the same footing as `GRAIN_NOT_ESTABLISHED`:
"unhandled," never "wrong."

### Test-first artifacts

1. `tests/lineage/test_value_domain_lattice.py`: the standard lattice-law
   and consistency-law property tests already used for the other
   properties, over a strategy generating `Unbounded | Bounded(...)`
   including the empty set.
2. `tests/lineage/test_value_domain_facts.py` and a bridge test alongside
   the existing contract-bridge tests: the discoverer's decision table
   (enabled test, disabled test, `where`-scoped test, string values, a
   decided non-string case, a missing `column_name`); the bridge's table
   (a bare `NominalEnum` producing a fact; a `UnitEnum` facet on a
   `DomainType` producing one on its bound column; a fixed facet producing
   none; two declarations on one column meeting rather than conflicting
   outright, matching the same posture `domain_type` uses).
3. `tests/lineage/test_value_domain_propagation.py`, one row per named
   transfer rule above, explicitly including `upper(status)` widening to
   top (the catch-all row, written first, since it's the one most likely
   to expose a generic-fold bug), a `CASE` remap keeping only the THEN
   literals, a searched `CASE` with no `ELSE`, an aggregate widening to
   top, a `UNION` of two enum'd sources unioning their sets, one bounded
   side unioned with an unbounded side producing top.
4. A data-as-judge property test against the DuckDB oracle: draw a small
   literal set, source rows from it plus NULLs, and a model from a grammar
   over passthrough, rename, union, `COALESCE`-with-literal, `CASE` remap,
   `upper()`, and a `WHERE ... IN` filter; assert every `Bounded` output
   set is a superset of the materialized distinct non-null values, and
   that a passthrough column's set exactly equals the source set (the
   anti-vacuity check).
5. `tests/check/test_dead_predicate.py` through the existing `run_check`
   harness with an in-test `NominalEnum`/`ModelContract` pair: the
   form × literal-class × context table above with expected fire/silent
   and a wording fragment; the CASE table (simple, searched, no `ELSE`,
   `ELSE NULL`, `ELSE 0`, a non-column arm); lineage-distance rows (direct,
   through a CTE, through a passthrough model, through `upper()` silenced,
   through a `UNION` of two enum'd sources with a stray only when absent
   from both); a tuva-shaped fixture: an enum bound to
   `encounter_type` on `encounters__combined_claim_line_crosswalk`, a
   downstream `where encounter_type = 'acute inpatent'` (a typo of
   `'acute inpatient'`) firing `DEAD_PREDICATE`, a `CASE` over the same
   column missing a member with no `ELSE` firing the coverage finding, and
   `sum(case when encounter_type = 'acute inpatient' then 1 else 0 end)`
   staying silent.
6. A property test against the same DuckDB oracle at the check layer: for
   a flagged always-false atom sitting in a top-level `WHERE`, the
   materialized result is empty; for always-true, the row count with and
   without the atom matches; for an unflagged member literal, a generator
   that includes that value produces a non-empty result. This is the iff
   that makes the check's decision procedure exact, not approximate.
7. After the suite is green, inject deliberate breaks and confirm each
   fails a named test: the catch-all rule removed (so `upper()` wrongly
   keeps the set), `CASE` folding its WHEN conditions into the set, `NULL`
   grounding the empty set instead of top, the case-fold wording rule
   inverted, an `ELSE`-literal CASE firing when it shouldn't.

### Sequencing and open questions

Suggested order: the lattice, property, discoverer, and bridge facts
together (this closes #135's propagation half and #36's accepted-values
half in one pass); then the check wiring and the two kinds. Everything
else is a genuine follow-up, not a prerequisite, and can land in any
order later: reading `WHERE`-narrowing through the predicate-flow
machinery so a declared narrow category contradicted by a wider upstream
domain becomes its own finding (the #135 contradiction case, distinct from
dead-predicate); `LIKE` decidability over a known member set; a
column-to-column dead join key via the meet of two sets; grounding a
native `CHECK (col IN (...))` constraint; and #36's range half
(`accepted_range`/`CHECK ... BETWEEN`), which is a separate interval
lattice and a separate section of the parent doc (§7).

Open for whoever implements this:

- Case sensitivity: exact match with a distinct "differs only by case"
  wording (leaning toward this), or fold case the way other nominal
  comparisons do? Warehouse collation makes this genuinely
  dialect-dependent, worth deciding rather than defaulting.
- A kind mismatch (`status = 1` against a string-valued set): silent, or
  fire? Leaning silent, since it is very likely a different bug class
  (wrong column) rather than this one.
- Whether `NEQ`/`NOT IN` strays belong under the same `ERROR` kind as the
  dead-predicate case, or a separate, quieter kind for the "constant-true"
  shape.
- Bare `bool` columns: ground `{true, false}` as a value domain, or skip
  them entirely? A boolean's set is fixed and rarely interesting on its
  own, so skipping may be the right default.
- Whether to retire #135 as its own ticket in favor of folding it into
  this property outright, since the propagation gap it names and the
  fact this property grounds are the same fact.

## Style note for whoever picks this up

Both plans use plain, closed decision tables on purpose: CLAUDE.md's
soundness section is explicit that a check branching on a closed type
(every `JoinSide`, every comparison operator, every literal class) has to
decide every case rather than handle one and let the rest fall through to
the wrong answer, and the tables above are meant to be copied close to
verbatim into the `match` statements and test parametrizations, not
loosely paraphrased. Please re-verify every file and line reference
against current `HEAD` before relying on it; this plan is a snapshot, not
a live index of the codebase.
