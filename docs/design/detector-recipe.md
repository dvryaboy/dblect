# Adding a detector: the file set and a rough line budget

A declaration-graded detector is a lattice, a way to ground it from what a user
declared, a way to propagate it through SQL, and a check reader that turns its
flow annotations into findings. The property kit
(`src/dblect/lineage/facts/kit.py`) and the located-finding helper
(`src/dblect/check/located.py`) exist so that shape is data and glue, not code
you write fresh each time. This note names the files a new detector touches and
about how big each one should be; a module coming in well over its budget is
usually building something the kit already has a piece for.

## The property module

`src/dblect/lineage/properties/<name>.py`. This is where the detector's meaning
lives: the value type, the lattice (`meet`/`join`/`top`/`bottom`), and the
transfer rules for the SQL operators and aggregates that matter to it.

A property whose facts come straight from the manifest (a dbt test, a native
constraint) reaches for `column_kit`/`relation_kit`: one call, fixing the
lattice and the transfer catalogs, and it derives the facts collector, the
grounding fold, the grounded-scopes and conflicts readers, and the
`Property` constructor. A property whose `ground` a caller supplies from
elsewhere (a typed contract bridge, as domain-type and functional-dependency do
today) reaches for the smaller `grounding_fold` instead, and builds its
`Property` by hand from `column_property`/`relation_property` when it needs a
semiring, a coherence guard, or a relation reducer the kit does not default.

Two transfer-rule factories live in the same module: `top_rule` for a node
whose result carries none of the property's information regardless of its
children (a comparison, in domain-type's reading), and `constant_aggregate` for
an aggregate whose meaning discards its child entirely (`COUNT`, always safe,
always the same answer whatever it counted).

Budget: 150-350 lines. The four migrated properties range from nullability at
about 300 (it also carries the outer-join taint and conditional-activation
machinery, which are lineage concerns, not kit-shaped ones) down to
functional-dependency's roughly 200 for the lattice plus entailment.

## The check reader

Either a new small module (`src/dblect/check/<name>.py`, the shape
`check/grain.py` uses for a relation-scoped finding with no single SQL line to
point at) or a reader function added next to the others in `check/run.py` for a
column-scoped, located finding.

A located reader is a generator yielding `LocatedRow`s (uid, the node or nodes
to read a compiled line from, the finding kind, the message, the column), fed
to `check/located.py`'s `locate_findings` with the sort key this detector's
findings should read in. That function does the file lookup, the compiled span,
and the source-template back-map; the reader's whole job is the walk and the
message. A reader that also needs a property's flow value for a scope the walk
might not have reached (a join key never itself projected) reaches for
`annotation_or_grounded` rather than writing its own fallback closure.

Budget: 40-100 lines for the reader plus its message-formatting helper.

## Registry wiring

`check/run.py`'s `propagate_world` builds every property once, assembles them
into one `PropertyRegistry`, and calls `dblect.lineage.property.run` a single
time over the graphs the registry's properties actually use. Adding a property
means adding it to that one tuple and to the `graphs` mapping if it needs a
scope kind not already there; reading its result is `store.scoped(prop.ref)`,
typed to the value and scope the property's own ref carries. There is no
per-property wiring beyond that: no new `AnnotationStore`, no new
`PropertyRegistry` built just to reach `.dep_context`.

## Tests

- **Propagation**: `tests/lineage/test_<name>_propagation.py`, a
  `PropagationCase` table (`tests/lineage/_propagation_table.py`) when the
  property's tests are one model over one set of declared facts on named
  source columns. A relation-scoped property whose tests each build a
  different multi-source manifest (a join's two sides, a cross-model chain)
  is not this shape, and stays one function per scenario; forcing it into the
  table would hide the manifest construction the test is actually about.
- **Lattice**: `tests/lineage/test_<name>_lattice.py`. One Hypothesis law test
  over the property's own strategy (`assert_lattice_laws`/
  `assert_consistency_laws` from `tests/lineage/_lattice_laws.py`), then only
  the examples that carry a domain-specific reading beyond the generic laws:
  what a specific `meet`/`join` computes, an invariant a constructor enforces,
  a "resolution never contradicts" property peculiar to this lattice's shape.
  An example whose assertion is a law with concrete numbers substituted in
  (`meet(top, x) == x` for one hand-picked `x`) is already proven, more
  generally, by the law test; it does not need its own function.
- **Check**: a `CheckCase` table (`tests/check/_check_table.py`) through
  `run_check` for the cluster of tests that share one manifest template and
  vary only the model's SQL, the expected finding kinds, and a wording
  fragment. A silent row (`expected=()`) still asserts the model built, so "no
  findings" is never confused with "nothing was analyzed". A test asserting
  something the table's four fields cannot express (a suppression directive,
  a coverage number, line provenance) stays its own function.
- **Empirical soundness**, when the property's claim is checkable against
  materialized data: `tests/lineage/test_pbt_<name>_soundness.py`, generating
  scenarios with Hypothesis and checking them through
  `tests/lineage/_duckdb_oracle.py`'s `assert_no_over_claims`, which
  materializes the generated tables and the model SQL once and asserts every
  claim's violation count is zero. The property supplies the generator, the
  SQL grammar, and the query that counts a violation of one claim; the
  materialize/teardown dance is not the property's to write. A property whose
  soundness argument is symbolic rather than empirical (functional-dependency's
  two-row entailment proof) does not use this harness at all.

Budget: propagation and lattice tests together land under 250 lines when the
property fits the table shapes above; a check-table cluster is a handful of
rows plus one parametrized test function, well under 100 lines for the part
that used to be three or four near-duplicate ones.

## What is still bespoke, deliberately

A decision-table detector (a closed function from a SQL shape to a finding
kind and its wording) generates its own tests from the table it declares, one
parametrized run per row through `run_check`; that pattern is not on `main`
yet; the first one to land should read that shape here once it exists. A
property's coherence guard, a relation reducer, or a semiring is always
hand-written in the property module: the kit derives the surrounding
scaffolding, not the algebra that makes a detector's finding actually true.
