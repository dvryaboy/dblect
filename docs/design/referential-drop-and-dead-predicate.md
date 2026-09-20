# Referential orphan drop and dead predicate: implementation plans

*Status: implementation plan, not yet built. `intent-supplying-contracts.md`
proposes seven contract shapes. Grain and functional dependency are built and
in live, uncommitted use against tuva-core. Referential integrity (the
orphan-drop half) and dead predicate are not, and the parent's estimate of
how much wiring each still needs is optimistic for dead predicate in
particular. This note is an audit of the source tree as of `8ebf61e` on
`main` plus one worked plan per check. Names are stable; line numbers are
not, so none are given.*

## Where this sits

`intent-supplying-contracts.md` (branch `docs/intent-supplying-contracts`,
not yet merged) is the parent document; its referential-integrity and
dead-predicate sections are reproduced in enough detail below that this note
stands on its own. Two other pieces are background rather than part of what
to build: the parent's conservation section shares its "walk the lineage from
a declared origin to a point of use, classifying each hop" framing with part
of the referential plan, and `refutation-and-verdicts.md` defines the verdict
vocabulary (proven, not-established, trusted, contradicted) both plans slot
into.

## Referential orphan drop

### The shape

A join that discards rows with no match on the other side is ordinary SQL,
correct almost everywhere, so flagging it undeclared is noise. Declare that a
column is a foreign key, though, and an inner join across it stops being
ambiguous: every child is expected to match a parent, so a silent drop is now
something the analysis can honestly report. The parent's example:

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

The substrate is complete; only the check is missing.
`ColumnProxy.references()` (`contracts/proxy.py`) builds a `ReferencesFact`,
which `types/bridge.py:_lower_references` resolves into a
`ForeignKeyEdge(child: ColumnRef, parent: ColumnRef)` with no provenance or
detail field yet. `foreign_key_edges()` merges contract-declared edges with
`dbt_relationship_edges()`, de-duplicated; its docstring calls itself "the
merge point a future fan-out finding or fixture generator reads from." Its
only references outside `bridge.py` are the package re-export and its tests.

One gap to fix alongside this: `dbt_relationship_edges` reads a
`relationships` test's `column_name`/`field`/`to` kwargs but never inspects
`tm.where`, so a `where`-scoped relationships test (a conditional claim) is
read as an unconditional edge. The edge should stay (a conditional test
still declares the relationship, and a join across it is still worth
reading), but it must not count as a guard; the decision below keeps the two
roles apart.

A second gap, in the manifest reader: `TestMetadata` carries `enabled` and
`where` but not the test's configured `severity`. A `relationships` test run
at `severity: warn` does not fail a build, which matters for the guard below.

### Architecture: a declaration-graded reader, not a grain-shaped emitter

Build it as a new module, `check/referential.py`, shaped like
`check/run.py:_join_key_findings`: a direct structural read over the
resolved, `ColumnRef`-stamped tree, gated on a declaration. Not shaped like
`check/grain.py`.

`grain.py` compares a *pre-reconciliation* derived value against a
declaration at the declaring model: what the SQL alone establishes, before
the declaration merges in. An orphan drop has no derived value to compare.
The SQL neither re-derives nor defeats the foreign key; it just contains a
join whose shape determines whether an unmatched child survives. Nor is a
`ResolvedPredicate` reader (the machinery the conservation section calls
for) a prerequisite: that reader is for a value that flows and gets
apportioned across a lineage path, and the orphan drop needs only a join
site and a declared edge. The shared reader can come later as a
generalization once conservation checks want it.

The builder already stamps every column reference, not only projected ones,
with a resolved `ColumnRef`: `lineage/builder.py`'s `_Walker` writes a
resolved ref back for a reference "in any position (an UNNEST argument, a
join key, a filter)," resolving through CTE and derived-table scopes. That
is what lets a join's `ON` clause resolve straight to the same `ColumnRef`
vocabulary a declared `ForeignKeyEdge` uses, with no separate name index and
no special handling for a join buried inside a CTE body (the tuva-core shape
below).

### A decision procedure closed over every join kind

`sql/_sqlglot.py` already separates the two questions a join raises.
`outer_join_optional_aliases` answers "which aliases are NULL-padded" and
`joins_with_outer_dropped_aliases` answers "which aliases lose unmatched
rows," per join, with its docstring giving the reason INNER reports the
empty set (an inner join's unmatched rows belong to no single side). That
reason is right for the nullability consumer it was written for and wrong
for this one: for an orphan check the question is "does the child's
unmatched row survive," and under INNER it does not, whichever side the
child sits on. So the extension is one function that answers both questions
over every `JoinSide`, with the two existing helpers becoming projections of
it:

```python
@dataclass(frozen=True)
class JoinRowEffect:
    join: exp.Join
    side: JoinSide
    optional: frozenset[str]           # aliases NULL-padded on unmatched rows
    dropped_unmatched: frozenset[str]  # aliases whose unmatched rows leave the result

def join_row_effects(sel: exp.Select) -> list[JoinRowEffect]: ...
```

decided by one `match side:` closed by `assert_never`, so every arm is
decided rather than one handled and the rest falling through:

| side  | optional                 | dropped_unmatched                                          |
|-------|--------------------------|------------------------------------------------------------|
| INNER | ∅                        | right ∪ accumulated_left                                   |
| LEFT  | {right}                  | {right}                                                    |
| RIGHT | accumulated_left         | accumulated_left                                           |
| FULL  | right ∪ accumulated_left | ∅                                                          |
| CROSS | ∅                        | ∅ (no match predicate, nothing is "unmatched")             |
| SEMI  | ∅                        | accumulated_left (a probe row without a match is dropped)  |
| ANTI  | ∅                        | ∅ (unmatched rows are exactly what it keeps)               |

The projections keep their current contracts exactly:
`outer_join_optional_aliases` is the union of the `optional` column, and
`joins_with_outer_dropped_aliases` reports `dropped_unmatched` for the LEFT
and RIGHT rows only and the empty set otherwise. That second one matters:
`detect_join_on_nullable_key` skips the aliases it returns, so a projection
that let INNER's row through would silence nullable-key findings on every
inner join. The existing tests of both helpers are the regression net.

The table is per join, and the hazard is not. Three shapes drop an orphan
that the join's own row says survives, and the first cut should name them as
a documented gap rather than half-handle them:

- `child LEFT JOIN parent ... WHERE parent.col = x`, which
  `WHERE_ON_OUTER_JOINED_NULLABLE` already reads for nullability;
- `child LEFT JOIN parent ... INNER JOIN other ON other.x = parent.y`, where
  the later join's `ON` references the padded side;
- a comma or `CROSS` join whose equality lives in `WHERE`.

### Files, functions, types

- **`types/bridge.py`**: give `ForeignKeyEdge` a `provenance: Declared` and a
  `detail: str | None`, so a finding can say which contract field or which
  dbt test grounds the edge, and so the dedup in `foreign_key_edges` can
  prefer the contract edge's detail when both exist. In
  `dbt_relationship_edges`, keep a `where`-scoped test's edge and carry the
  condition on it (`condition: str | None`), so the declaration survives.
  Add `relationship_tested_edges(manifest)`, the guard set, computed
  directly from the tests (enabled, `tm.where is None`, `error` severity)
  and never from the merged, de-duplicated edge list: dedup keeps one edge
  per child/parent pair and would otherwise lose the fact that a
  contract-stated edge is also test-covered.
- **`manifest/parse.py`**: carry the test's configured `severity` on
  `TestMetadata`, so the guard can require an `error`-severity test. Until it
  does, the guard assumes default severity and says so in its docstring.
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
     `{edge.child, edge.parent}` for some declared edge. Other conjuncts (a
     second key column, a literal pin) narrow the match, so the join drops a
     superset of the orphans: rows whose foreign key does hold but whose
     other conjunct fails. The finding stays true (orphans vanish here), but
     the message must not imply the FK is the only way a row leaves.
  4. Fire only if the child's alias is in `effect.dropped_unmatched`.
  5. Fire only if the edge isn't already covered by an enabled,
     unconditional, error-severity `relationships` test (see the verdict
     discussion below). A guarded edge with extra conjuncts
     (`ON c.region_id = r.region_id AND r.active`) is silent by design: the
     `active` filter drops rows even while the FK holds, but no declaration
     speaks to `active`, so that drop is the "intended filter" this check
     exists not to flag. The check reports the FK-attributable drop and
     nothing else.
  6. Nullability is out of scope here; `detect_join_on_nullable_key` owns
     the NULL-key drop, this finding is about a non-null key with no
     parent. Both can fire on the same join.
- **`check/findings.py`**: a `REFERENTIAL_ORPHAN_DROP` kind.
- **`severity.py`** (top-level `dblect/severity.py`): `WARN`. A new
  `CheckFindingKind` is a type error under the closed `match` until it has a
  level, and both `tests/test_fail_threshold.py` and
  `tests/audit/test_suppress.py` parametrize over `list(CheckFindingKind)`,
  so the enumeration suites pick the kind up with no test edits. The
  `_check_severity` docstring today defines error as "declared and computed
  disagree" and warn as "could not see enough to judge"; this kind is
  neither, so add a third clause there: a hazard whose trigger is a data
  violation the SQL turns silent.
- **`check/run.py`**: thread the merged edges and the guard set through
  `CheckGraphs`, call the new reader alongside `_join_key_findings`.

Suggested wording, never "violated": *"this JOIN discards rows of
`stg_orders` whose non-null `region_id` has no match in `dim_regions`.
`region_id` is declared a foreign key to `dim_regions.region_id`, so no such
rows are expected; if that ever stops holding, they vanish here with
nothing reporting it. Guard the edge with a `relationships` test so a
violation fails loudly, or switch to a LEFT JOIN and handle the unmatched
side explicitly."* When the `ON` carries other conjuncts, append: *"the join
also requires `data_source` to match, so rows can leave here even while the
foreign key holds."*

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
violation into a silent drop instead of a loud one. That's a hazard grade,
`WARN`, never `ERROR`, because the data may satisfy the FK everywhere the
whole life of the pipeline, in which case the inner join is simply correct
code. The wording above never calls the FK "not established," unlike the
grain check, where the SQL really can carry positive evidence against the
declaration (a strictly finer key surviving to the output). Here the SQL
carries no such evidence either way.

The guard exists because otherwise every inner join across every tested FK
in an ordinary star schema fires, which is exactly the noise this check
exists to avoid. A tested edge already has a loud failure mode (the test
fails), so the entire content of this finding, that the failure mode is
currently silent, is already false for it. That is only true of a test that
is enabled, unconditional, and at `error` severity, hence the three
conditions in step 5.

One consequence worth stating: an edge that exists only because of a
`relationships` test is always silent under this guard, so the check fires
only on contract-declared edges with no covering test. That matches the
bootstrap skill's guidance not to restate in a contract what a dbt test
already says.

### Two tuva-core fixtures that pin the line this check has to draw

`encounters__stg_medical_claim.sql` inner-joins every claim line onto two
models inside its `final` CTE:

```sql
inner join {{ ref('service_category__service_category_grouper') }} as g
  on m.claim_id = g.claim_id
  and m.claim_line_number = g.claim_line_number
  and m.data_source = g.data_source
  and g.duplicate_row_number = 1
inner join {{ ref('encounters__patient_data_source_id') }} as d
  on m.person_id = d.person_id
  and m.data_source = d.data_source
```

Both are composite, and the first carries a literal pin, so both exercise
step 3's narrowing rule. They read almost identically, and the check needs
to treat them oppositely:

- `encounters__patient_data_source_id` is `select distinct person_id,
  data_source from normalized__medical_claim union distinct ... eligibility`,
  so the edge from a claim's `(person_id, data_source)` into it is total by
  construction (only a NULL key fails, which is the nullable-key path this
  check doesn't own). Once `person_id` carries a declared or dbt-sourced
  `ForeignKey`, this join is the genuine positive fixture, and it is also
  the fixture for the composite-key open question below: the single-column
  edge is a projection of the pair that is actually total.
- `service_category__service_category_grouper` is a `union all` of arms each
  filtered to one `claim_type` (`'professional'`, `'institutional'`,
  `'undetermined'`), so a claim line with any other `claim_type` has no row
  there and the inner join drops it. Nothing in the model says so; it falls
  out of the arms. A declared *total* FK there would be a false
  declaration, not a missing one, which is the parent's point made
  concrete: an inner join is "indistinguishable from an intended filter"
  absent a declaration, and here the filter really is intended. This
  fixture must stay silent, both because nothing is declared today and
  because declaring a total FK against it would be wrong.

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
   plain LEFT row; a composite and a literal-pinned `ON` firing with the
   narrowing sentence; an equality under `OR` silent; `USING` silent
   (documented miss); the three per-join gaps above silent (documented
   miss); the join inside a CTE body whose FROM is the child model (the
   tuva shape) firing; no edge declared silent (the tuva grouper shape); a
   relationships-test-only edge silent, contract-only firing, `where`-scoped
   test firing (not a guard), disabled test firing, `severity: warn` test
   firing once severity is parsed; the NULLABLE-key case producing both
   this finding and `JOIN_ON_NULLABLE_KEY` together; message pins including
   `"violat" not in message.lower()`.
3. A data-as-judge property test reusing the existing DuckDB oracle
   fixture, over the fragment the check claims: one declared, unguarded
   edge, an `ON` that is a conjunction of column equalities containing the
   edge, and no anti-join idiom. Generate a parent table, a child table
   with at least one orphaned key and at least one matched key, and a join
   drawn from a grammar over every `JoinSide` and child position that
   projects a child identifier whenever the side allows it. Assert the
   check fires if and only if the materialized output contains the matched
   child row and lacks the orphaned one. SEMI and ANTI with the child on
   the right project no child column, so for those the oracle compares
   against the same query with the join relaxed to LEFT. `USING`,
   `NATURAL`, and an equality under `OR` are outside the fragment and stay
   in the documented-miss rows of the table test above. This is the exact
   decision procedure for "does this join discard unmatched children," with
   the warehouse as the oracle, and it has to fail on any of the seven
   `JoinSide` arms being mis-decided.
4. Once the suite is green on its first run, inject deliberate breaks and
   confirm each fails a named test: SEMI treated like ANTI, INNER dropping
   only its right side, the guard gate inverted, an `OR`-leaf treated as a
   conjunct.

### Sequencing and open questions

Suggested order: the `ForeignKeyEdge` provenance and `where`-handling fix,
then the `join_row_effects` generalization, then the check and its wiring;
test-severity parsing can land before or after. A natural follow-up, not a
prerequisite, is a carrier property so a declared edge survives a
rename-only passthrough model rather than only matching an exact
`ColumnRef`, and a further one is threading the edges into the existing
undeclared detectors so a fan-out or nullable-key finding can name the
relationship it threatens (the parent's "sharpens their framing" point).

Open for whoever implements this:

- Silence only when a `relationships` test already covers the edge
  (recommended), or fire always at `WARN` and simply name the covering
  test if one exists? The second needs a second, quieter kind or an
  `INFO` grade, since severity is assigned per kind.
- Whether the three per-join gaps above get a second cut that reclassifies
  the join as effectively INNER for the edge, or stay documented misses.
- Whether to resolve `USING (k)` in the first cut or document it as a miss;
  it needs the shared column's stamp to exist, which may not be guaranteed
  today.
- Composite keys: both `ForeignKey` and dbt `relationships` are
  single-column, so a composite `ON` clause is judged per single-column
  edge; the narrowing sentence in the message is how the first cut says so.

## Dead predicate over a closed value set

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

### Why this needs more than the parent estimates

`intent-supplying-contracts.md` calls this "small, and it rides the AST the
proxy already builds and the set #135 or #36 carries." Both of those are
still open issues, not shipped facts to ride on:

- **#135** ("propagate standalone closed-category columns") already
  diagnoses the exact gap: a bare `status: OrderStatus` annotation *is*
  classified as `FieldKind.NOMINAL` in `types/contract.py:_build_declaration`,
  but `bridge.py:_resolve_one` routes it to `ScalarDecl`, which today
  "carries no fact... in this build." Recognized, then inert.
- **#36** ("accepted-values and range discoverers") is the zero-declaration
  companion, grounding a set from a dbt `accepted_values` test or a native
  `CHECK ... IN`. Also unstarted; the only traces in the tree are a
  provenance-enum comment naming `accepted_values` as a known dbt test
  kind, and a sentence in `bootstrap/skill.md` saying an `accepted_values`
  test "becomes a value domain," which is drift to correct (or make true)
  as part of this work.

So there is, as of today, no lineage fact anywhere for a "read the set at a
`Compare`/`InSet`/`CASE`" check to read, no matter how the set gets
declared. The right scope is a small, new `ValueDomain` lineage property
that closes both issues in one pass rather than either half alone.

`NominalEnum`/`UnitEnum` today (`types/enums.py`) are plain `enum.StrEnum`
marker bases, used only as a facet riding *inside* a `DomainType` (tuva-core's
`ClaimAmountBasis(UnitEnum)` on `ClaimAmount`, for instance), never bound
directly to a bare column. #135's own fix belongs on this same property
rather than as a `DomainTag` extension: a `DomainTag`'s nominal facet
carries an identity, not a member set, and the dead-predicate check needs
the members.

### The lattice, and what the empty set means

`ValueDomain = Unbounded | Bounded(values: frozenset[Lit])`, reusing `Lit`
from `lineage/predicate.py` so `1` and `'1'` stay distinct. A value domain
describes a column's *non-null* values. `Unbounded` is top (no claim),
`Bounded(∅)` is bottom, join is union, meet is intersection.

Bottom is a legitimate value, not a contradiction marker. A column that is
always NULL has no non-null values, so its domain is `Bounded(∅)`, and
`col = 'x'` against it really is dead (`NULL = 'x'` is never true). Two
trusted declarations whose sets are disjoint also meet to `Bounded(∅)`; that
one *is* a contradiction between declarations and should surface as a
`CONTRACT_ISSUE` at grounding rather than propagate silently.

Every transfer rule is named and closed rather than inferred by a generic
fold:

| node                       | domain                                                        |
|----------------------------|---------------------------------------------------------------|
| `Column`, `Alias`          | pass through                                                  |
| literal                    | singleton                                                     |
| `NULL` literal             | `Bounded(∅)` (no non-null values; the identity for union)     |
| `COALESCE(a, b, ...)`      | union of children                                             |
| `CASE ... END`             | union of every THEN branch and the ELSE; a missing ELSE contributes `Bounded(∅)`, never the WHEN conditions |
| `CAST`                     | top                                                           |
| `UNION`                    | union                                                         |
| aggregate                  | top (`MIN`/`MAX` could pass through; leave for a follow-up)   |
| any other `exp.Expression` | top                                                           |

The `NULL` row is the one most likely to be gotten wrong: making it top
would widen every ELSE-less `CASE` and every `COALESCE(x, NULL)` to
`Unbounded`, silencing the check on exactly the shapes the parent's example
uses. The catch-all row is what keeps the property sound by default (string
functions, arithmetic, windows all widen rather than fold), and the property
test below treats those two rows as the highest-value ones to pin.

### Files, functions, types

- **`lineage/properties/value_domain.py`** (new; `domain_type.py` is the
  template for the `join_key_conflicts`-shaped pure-signal pattern both this
  and the referential reader follow): the lattice and transfer table above.
- **`lineage/facts/model.py`** or a sibling discoverer module:
  `accepted_values_discoverer()`, reading an enabled `accepted_values` test
  the same way the existing `unique`/`not_null` discoverers read theirs,
  producing a `Fact[ValueDomain, ColumnRef]` with `Declared(DBT_GENERIC_TEST)`
  provenance. Decide explicitly (not silently) what happens for a
  `where`-scoped test and for a value list that is neither string nor
  numeric (`Lit` has only those two kinds today).
- **`types/bridge.py`**: in `_resolve_one`, a `ScalarDecl` whose field binds
  a `NominalEnum` (or a `DomainDecl` whose bound enum facet isn't fixed)
  grounds a `ValueDomain` fact on that column (for a facet, on the
  companion column it binds to), closing #135's propagation gap and
  removing the "`ScalarDecl` carries no fact" comment. Two declarations on
  one column meet; a disjoint pair is a `CONTRACT_ISSUE`.
- **`check/dead_predicate.py`** (new): reads the propagated `ValueDomain`
  at each stamped column reference and applies the decision tables below.
  Four kinds: `DEAD_PREDICATE`, `DEAD_PREDICATE_CASE_ONLY`,
  `REDUNDANT_PREDICATE`, and `CASE_LEAVES_ENUM_MEMBER_UNHANDLED`.
- **`check/run.py` / `check/findings.py` / `severity.py`**: propagate the
  new property over the column graph, wire the reader in, add the kinds and
  their severities.
- **`bootstrap/skill.md`**: make the `accepted_values` sentence true, and
  drop "a standalone category does not yet propagate on its own" once it
  does.

### The decision tables

The domain covers non-null values only, so every verdict below is stated in
three-valued terms first and collapsed to two only where SQL itself does.
A stray literal makes `col = 'x'` **never true**: FALSE on a non-null row,
UNKNOWN on a NULL row. A filtering context (`WHERE`, `HAVING`, `QUALIFY`,
`JOIN ... ON`, a `CASE WHEN` condition) treats UNKNOWN as FALSE, so there
"never true" becomes "always empty" or "never taken." A projected scalar or
an intermediate boolean does not collapse, so there the wording stays
"never true," not "always false."

*Comparison form*: `EQ` with a stray literal is never true;
`NullSafeEQ` (`IS NOT DISTINCT FROM`) with a stray non-null literal is
constant FALSE outright, since it has no UNKNOWN case; `NEQ` with a stray
literal is true on every non-null row and UNKNOWN on a NULL row, so it
"filters only NULL rows," never "always true," and `NullSafeNEQ` is constant
TRUE; `IN (...)` reports each stray literal individually, "never true" only
if every one is stray; `NOT IN (...)` with a stray literal is a no-op for
that literal only when the list has no `NULL` member (a `NULL` in the list
makes `NOT IN` never true on its own, a different hazard this check leaves
alone and must not call a no-op); ordering comparisons (`<`, `<=`, `>`,
`>=`, `BETWEEN`), the `LIKE` family, and `IS` are silent, since a nominal
set carries equality only; a column-to-column equality is silent in this
first cut (a natural follow-up: meet the two columns' sets at a join key,
the same shape `_join_key_findings` already reads for domain types).

*Literal class*: an exact member is silent; a non-member of the same kind
under `EQ`/`NullSafeEQ`/`IN` fires `DEAD_PREDICATE`, and under
`NEQ`/`NullSafeNEQ`/`NOT IN` fires `REDUNDANT_PREDICATE`; a string that
differs from a member only by case fires `DEAD_PREDICATE_CASE_ONLY`, a
separate kind because whether it is dead depends on the warehouse's
collation; a kind mismatch (a numeric literal against a string-valued set)
is a case to decide explicitly rather than default into either silence or a
fire; a literal `NULL` operand, in any form including `NullSafeEQ(col,
NULL)` (which selects the NULL rows), is silent.

*Boolean context*, found by walking up from the comparison to its nearest
owner: a top-level `WHERE`/`HAVING`/`QUALIFY` conjunct reads "the result is
always empty" (or "filters only NULL rows" for the redundant case); a
`JOIN ... ON` conjunct reads "never matches"; under `OR` it's "this
disjunct never matches"; under `NOT` the polarity flips, and the kind flips
with it (`NOT (col = 'stray')` is redundant, not dead); inside a `CASE
WHEN` it's "this arm is never taken"; as a bare projected scalar it's
"never true."

*CASE coverage*: only decidable when every arm compares one bounded column
against a literal (a simple `CASE col WHEN ...` or a searched `CASE` whose
every arm is `col = lit`/`col IN (...)`); any arm of another shape makes
coverage unknown for the whole CASE, though a dead individual arm still
fires on its own. The parent's motivating case is a member falling silently
to a non-null `ELSE` (`'delivered'` routed to `'Other'`), so a non-null
`ELSE` must not silence the finding. The shape to spare is the indicator
idiom, `sum(case when status = 'cancelled' then 1 else 0 end)`, which is a
deliberate complement: a `CASE` with exactly one arm is silent, and so is a
`CASE` whose every THEN and ELSE literal lies outside the column's own
domain (it is computing something about the column, not remapping it). A
multi-arm remap with a missing member fires whether the default is absent,
`ELSE NULL`, or `ELSE 'other'`.

### Verdict grade: ERROR for the dead predicate, WARN for the other three

`DEAD_PREDICATE` gets the strongest grade in this pair of plans. The
literal's singleton meets the column's declared or grounded set to bottom
at the comparison itself: a genuine contradiction between two meaning
claims (the enum's intensional claim about what the column means, and the
literal's own claim), true regardless of what data exists. That's the same
license `DOMAIN_TYPE_CONTRADICTION` already uses, so: **`ERROR`**. Two
honesty caveats belong in the finding's own wording: the claim is exact
relative to the declared or discovered set, so a stale declaration produces
a finding that is right about the declaration and wrong about the data (in
which case the accompanying `accepted_values` test, if any, would also be
failing); and the approach is sound only because every unmodeled operator
widens to top rather than narrows, which is what the property test's
catch-all row pins.

The grade does not depend on whether the set came from a contract enum or
from an `accepted_values` test. Both are declarations that can go stale,
and the repo already trusts a `unique` or `not_null` test as a key or
nullability fact at full strength, so grading a test-grounded set lower
would make this the one dbt test read with less trust than its siblings. If
calibration against real projects shows the test-grounded half over-firing
(the fanout pair's history in `severity.py`), the split to reach for is a
provenance-keyed kind, not a quieter grade for the whole check.

`DEAD_PREDICATE_CASE_ONLY` is **`WARN`**. On a case-sensitive warehouse
(Snowflake and DuckDB by default) it is as dead as the plain kind; on a
case-insensitive collation it is live. Until the analysis reads the
adapter's collation and can grade it per dialect, `ERROR` would be wrong on
some warehouses, and the fanout pair is again the precedent for holding a
real hazard at `WARN` while it can over-fire.

`REDUNDANT_PREDICATE` is **`WARN`**. A `where status != 'shipd'` excludes
nothing the author meant to exclude, which is a real bug, but the SQL is
not contradictory and the result is not wrong rows by the analysis's own
definition of error, only unfiltered ones. It is a separate kind rather than
an arm of `DEAD_PREDICATE` because its wording ("filters only NULL rows")
and its grade both differ.

`CASE_LEAVES_ENUM_MEMBER_UNHANDLED` is decidable but intent-dependent (a
remap may deliberately lump a member into its default), so it gets
**`WARN`**, on the same footing as `GRAIN_NOT_ESTABLISHED`: "unhandled,"
never "wrong."

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
   `DomainType` producing one on its companion column; a fixed facet
   producing none; two overlapping declarations on one column meeting to
   their intersection; two disjoint declarations producing a
   `CONTRACT_ISSUE`).
3. `tests/lineage/test_value_domain_propagation.py`, one row per transfer
   rule in the table above, written in this order: `upper(status)` widening
   to top (the catch-all row, most likely to expose a generic-fold bug); a
   `NULL` literal grounding `Bounded(∅)`; a searched `CASE` with no `ELSE`
   keeping exactly its THEN literals; a `CASE` remap keeping only the THEN
   and ELSE literals, never the WHEN conditions; `COALESCE(status, NULL)`
   keeping the set; an aggregate widening to top; a `UNION` of two enum'd
   sources unioning their sets; one bounded side unioned with an unbounded
   side producing top; a column that is only ever `NULL` grounding
   `Bounded(∅)`.
4. A data-as-judge property test against the DuckDB oracle: draw a small
   literal set, source rows from it plus NULLs, and a model from a grammar
   over passthrough, rename, union, `COALESCE`-with-literal, `CASE` remap
   with and without `ELSE`, `upper()`, and a `WHERE ... IN` filter; assert
   every `Bounded` output set is a superset of the materialized distinct
   non-null values, and that a passthrough column's set exactly equals the
   source set (the anti-vacuity check).
5. `tests/check/test_dead_predicate.py` through the existing `run_check`
   harness with an in-test `NominalEnum`/`ModelContract` pair: the
   form × literal-class × context table above with expected kind and a
   wording fragment, including `NullSafeEQ` with a stray literal, `NOT IN`
   with a `NULL` list member staying silent, `NOT (col = 'stray')` firing
   the redundant kind, and a projected `col = 'stray'` worded "never true";
   the CASE table (simple, searched, no `ELSE`,
   `ELSE NULL`, `ELSE 'other'` firing, the one-arm indicator silent, an
   all-literals-out-of-domain `CASE` silent, a non-column arm); lineage
   rows (direct, through a CTE, through a passthrough model, through
   `upper()` silenced, through a `UNION` of two enum'd sources with a stray
   only when absent from both); a tuva-shaped fixture: an enum bound to
   `encounter_type` on `encounters__combined_claim_line_crosswalk`, a
   downstream `where encounter_type = 'acute inpatent'` (a typo of
   `'acute inpatient'`) firing `DEAD_PREDICATE`, `= 'Acute Inpatient'`
   firing the case-only kind, a `CASE` over the same column missing a
   member with `ELSE 'other'` firing the coverage finding, and
   `sum(case when encounter_type = 'acute inpatient' then 1 else 0 end)`
   staying silent.
6. A property test against the same DuckDB oracle at the check layer, with
   generators that include NULL rows: for a `DEAD_PREDICATE` atom in a
   top-level `WHERE`, the materialized result is empty; for a
   `REDUNDANT_PREDICATE` atom, the result equals the same query with the
   atom replaced by `col IS NOT NULL`; for a projected dead atom, no row
   carries TRUE; for an unflagged member literal, a generator that includes
   that value produces a non-empty result. This is the iff that makes the
   check's decision procedure exact, not approximate.
7. After the suite is green, inject deliberate breaks and confirm each
   fails a named test: the catch-all rule removed (so `upper()` wrongly
   keeps the set), `CASE` folding its WHEN conditions into the set, `NULL`
   grounding top instead of `Bounded(∅)`, the case-only kind collapsed into
   the plain one, `NEQ` reported as dead, `NOT IN` with a `NULL` member
   reported as a no-op, the one-arm indicator firing.

### Sequencing and open questions

Suggested order: the lattice, property, discoverer, and bridge facts
together (this closes #135's propagation half and #36's accepted-values
half in one pass); then the check wiring and the four kinds; then the
skill.md correction. Everything else is a follow-up and can land in any
order later: reading `WHERE`-narrowing through the predicate-flow
machinery so a declared narrow category contradicted by a wider upstream
domain becomes its own finding (the #135 contradiction case, distinct from
dead-predicate); `LIKE` decidability over a known member set; a
column-to-column dead join key via the meet of two sets; `MIN`/`MAX`
passing the set through; grounding a native `CHECK (col IN (...))`
constraint; and #36's range half (`accepted_range`/`CHECK ... BETWEEN`),
which is a separate interval lattice and the parent's range-violation
section.

Open for whoever implements this:

- A kind mismatch (`status = 1` against a string-valued set): silent, or
  fire? Leaning silent, since it is very likely a different bug class
  (wrong column) rather than this one.
- Bare `bool` columns: ground `{true, false}` as a value domain, or skip
  them entirely? A boolean's set is fixed and rarely interesting on its
  own, so skipping may be the right default.
- Whether to retire #135 as its own ticket in favor of folding it into
  this property outright, since the propagation gap it names and the
  fact this property grounds are the same fact.
