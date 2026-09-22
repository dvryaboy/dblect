# Adding a detector

A detector answers one question about a dbt project's SQL, using what the
user declared: "does this join drop rows the declared foreign key says cannot
exist?", "is this literal outside the column's declared value set?", "does this
aggregate mix currencies?". This note explains the pieces a detector is made of,
which of those pieces already exist in shared form, and where a new detector
plugs in. Read it before starting a detector, and read one existing detector
alongside it: `nullability.py` is the simplest complete example, and the
orphan-drop check is the simplest check that has no property of its own.

## The four pieces

Most detectors have four parts.

1. **A property**: the thing we track about each column or relation, and how
   it changes as SQL transforms the data. Nullability tracks "can this be
   NULL". Value domain tracks "which values can this hold". A property is a
   small algebra: a value type, a rule for combining two claims about the same
   thing (the meet), a rule for combining values that arrive from two branches
   (the join), and one transfer rule per SQL construct that matters.
2. **Starting facts**: where the property's initial values come from. Usually a
   dbt test (`not_null`, `accepted_values`, `relationships`) or a Python
   contract. A discoverer reads the manifest and produces facts; the contract
   bridge produces facts from contracts.
3. **A check**: code that reads the propagated values (or, for some detectors,
   the SQL tree directly) and decides what to report, with a finding kind, a
   severity, and a message a dbt user can act on.
4. **Tests**, at four levels, described at the end.

Some detectors skip the property. The orphan-drop check reads join structure
and declared foreign-key edges straight off the tree; it has a check and tests
and nothing else.

## The property module

Lives in `src/dblect/lineage/properties/<name>.py`. It contains the value type,
the lattice, and the transfer rules. That is the detector's meaning, and it is
always hand-written.

Everything around it is shared. `src/dblect/lineage/facts/kit.py` provides:

- `column_kit(...)` and `relation_kit(...)`. Give one of these the property's
  name, lattice, operator rules, and aggregate rules, and it hands back an
  object that can collect facts from discoverers (`.facts`), ground them into
  starting annotations (`.grounding`), report which scopes actually got a
  declared value (`.grounded_scopes`), report which scopes have contradictory
  declarations (`.conflicts`), and build the `Property` (`.property`). A
  property module built this way is one `column_kit` call and a short function
  that runs its discoverers and returns `.property(facts)`. Nullability is
  exactly this.
- `grounding_fold(lattice)`, the smaller version for a property whose facts
  come from somewhere other than the manifest. Domain type and functional
  dependency get their facts from the contract bridge, so they use this and
  build their `Property` by hand with `column_property` / `relation_property`,
  which also lets them pass the extra pieces the kit does not default (a
  semiring, a coherence guard, a relation reducer).
- `top_rule(lattice)`, the transfer rule for a SQL node that says nothing about
  the property no matter what its inputs said. A comparison's result carries no
  currency, so domain type binds `top_rule` to comparisons.
- `constant_aggregate(value)`, the transfer rule for an aggregate that ignores
  its input entirely. `COUNT` is the usual case: whatever it counted, the result
  is a non-null number.

If you find yourself writing a facts collector, a grounding fold, a "which
scopes grounded" reader, or a conflicts scan inside a property module, stop:
the kit already has it, and the copy will drift.

## Starting facts

A discoverer is a small class with one `discover(manifest, name_to_source)`
method that returns facts. Look at nullability's `not_null` discoverer or value
domain's `accepted_values` discoverer. Two helpers in
`src/dblect/lineage/facts/grounding.py` do the tedious part of mapping a dbt
generic test onto the column or relation it targets: `generic_test_column_ref`
and `generic_test_source_ref`.

Contract-declared facts come through `src/dblect/types/bridge.py`. If your
property has a contract form (an enum field, a foreign key marker), add a case
to the closed `match` in `_resolve_one` and produce facts there. Column names
are lower-cased whenever a `ColumnRef` is built, because that is how the
lineage keys them; a fact built with the contract's own spelling silently
matches nothing (#291 brings the bridge's older sites in line).

Two declarations can disagree. `.conflicts(facts)` names the scopes whose
declarations cannot all be true. Report those as a contract issue and leave
them out of the facts you ground, so one bad declaration cannot stop every
other column from being analyzed.

## The check

A check that points at a line of SQL is a generator that yields `LocatedRow`s:
the model, the SQL node to read the line from, the finding kind, the message,
and the column. Hand that generator to `locate_findings` in
`src/dblect/check/located.py` with the sort order you want. It looks up the
file, reads the compiled line span, maps it back onto the model's source
template, and sorts. Your reader only walks and writes the message.

If your reader needs a property's value for a scope the propagation may never
have reached (a join key that is compared but never projected), use
`annotation_or_grounded`: it returns the propagated value where there is one
and the scope's own declared value otherwise.

A check with no single line to point at (a relation-level grain claim) is a
small module of its own, in the shape of `check/grain.py`.

Wiring, all in `src/dblect/check/run.py`:

- `build_check_graphs` collects your starting facts once per run, so they are
  shared across the flag worlds the enumerator explores.
- `propagate_world` builds every property and registers them in one
  `PropertyRegistry`, then calls `run` once. Add your property to that tuple.
  Read its result back with `store.scoped(prop.ref)`; the type follows from
  the ref.
- `world_findings` is where per-model findings are produced. Add your reader
  there. Findings that are the same in every world still belong there, because
  the flag-world enumerator reads only this function.
- Add the finding kind to `CheckFindingKind` in `check/findings.py` and a case
  to the `match` in `severity.py`. The `match` is closed on purpose: a kind
  without a severity is a type error, not a runtime surprise.

## Messages

The message is the product. Write it for a dbt developer who has never read
this repository: name the model, the column, the declared fact, and what to do.
Do not say "lattice", "annotation", "meet", or "propagated". Never claim more
than the analysis proved: a join that hides a foreign-key violation has not
shown the key is broken, so its finding says the violation would be silent, not
that it exists.

## Tests

- **Lattice**: `tests/lineage/test_<name>_lattice.py`. One Hypothesis test
  running `assert_lattice_laws` (and `assert_consistency_laws` if the property
  is compared against declarations) over a strategy for your value type. Then
  only the examples that say something specific to this property: what
  `meet` computes for two particular value sets, an invariant a constructor
  keeps. Do not add an example that is just a law with numbers filled in; the
  law test already covers it.
- **Propagation**: `tests/lineage/test_<name>_propagation.py`. A table of
  `PropagationCase` rows (`tests/lineage/_propagation_table.py`): one model's
  SQL, the declared facts on its source columns, and the expected annotation
  on an output column. One row per transfer rule, plus the shapes that combine
  them (a rename through a CTE, a union, a CASE). A property whose tests each
  need a different multi-model manifest does not fit the table; write those as
  functions.
- **Check**: `tests/check/test_<name>.py`. A table of `CheckCase` rows
  (`tests/check/_check_table.py`): the model SQL, the finding kinds expected in
  order, fragments the message must contain (`wording`) and must not
  (`absent`). A row with no expected findings also asserts the model built, so
  a silent row cannot pass because the analysis never ran. If the check is a
  closed decision (comparison form × context → kind and wording, as dead
  predicate is), enumerate every cell as a row rather than sampling a few.
- **Data as judge**: `tests/lineage/test_pbt_<name>_soundness.py`, or under
  `tests/check/` for a check-level claim. Hypothesis generates small tables and
  SQL; `assert_no_over_claims` in `tests/lineage/_duckdb_oracle.py` builds
  them in DuckDB, runs the SQL, and checks that every claim the analysis made
  is true of the actual result. You write the generator, the SQL grammar, and
  the query that counts violations of one claim; the harness does the rest.
  The `oracle_con` fixture in the root `tests/conftest.py` shares one DuckDB
  connection across examples. When a claim has both a firing and a silent
  side, test both against the data: the expected verdict should come from the
  warehouse, not from a table you wrote by hand.

Write the tests first. When the suite passes on its first run, break the
implementation on purpose (flip a severity, drop a guard, swap two cells of a
decision table) and confirm a named test fails for each break.

## What the kit does not do

It does not write the algebra. Domain type's dimensional arithmetic,
functional dependency's entailment closure, dead predicate's CASE-coverage
reader: those are what make each finding true, and they are hand-written and
sized by the problem. The kit removes the copies that used to surround them.
