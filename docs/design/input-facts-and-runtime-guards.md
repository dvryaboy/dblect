# Facts about inputs: what tuva-core's data quality layer asks of the DSL

*Status: design notes from reading `tuva-core/models/data_quality` against the
DSL as built, with two experiments run on the tuva-core integration manifest.
Nothing here is tuva-specific machinery for dblect to grow; tuva-core is the
worked example that makes each gap concrete. Companion to
[declaration-dsl.md](declaration-dsl.md), [dsl-reference.md](dsl-reference.md),
and [referential-drop-and-dead-predicate.md](referential-drop-and-dead-predicate.md).*

## Why this note exists

A dbt package that consumes customer data has to state what it expects of that
data, and tuva-core states it twice. Once in prose, in the Input Layer YAML
descriptions ("header-level fields should be consistent across all lines for
the same claim_id"). Once executably, in the data quality layer, as a few
hundred hand-written SQL flags evaluated at run time over the real rows. Those
expectations are exactly the facts dblect wants to hold as axioms about a
pipeline's sources: what is a key, what determines what, what references what,
what is never null and under which condition. This note asks what the DSL
would need so a project like tuva-core could write those expectations once, in
a form the static analysis reads forward and a runtime guard checks backward.

## What tuva-core asserts over its inputs

Two opt-in tiers, both materialized as dbt models rather than tests, so they
report and never block. **Structural** checks, per Input Layer model and data
source, that required columns exist, types match, the table is populated, and
the composite primary key is non-null and unique. A failed prerequisite yields
"not evaluated" rather than pass or fail. **Logical** checks are tri-state
integer flag columns in per-grain flag models, each built by one macro that
takes a failure predicate and a separate applicability predicate: NULL when
the row is not in scope, 0 pass, 1 fail. A Jinja registry carries each test's
name, description, type, severity, and affected columns, and per flag model a
grain plus key tuple.

| test_type | count | what it asserts |
|---|---|---|
| invalid | 147 | value in a literal set, in a terminology table, in a date range, or of a format |
| missing | 77 | not null, often under a condition (admission date only for inpatient bill types) |
| referential | 42 | left join to a parent, null check, always scoped by data_source |
| consistency | 35 | one value per claim across its lines, or an implication (death flag implies death date) |
| temporal | 13 | start before end, non-overlapping spans via window functions |

Three choices in that design are worth adopting outright. Separating
applicability from failure is precisely the `where` scope the DSL documents,
and it prevents the vacuous pass: a single-line claim is "not applicable" to
a one-value-per-claim check rather than a pass. Attaching a grain and key
tuple to each flag model is the same relational framing dblect gives a fact
that ranges over a key. And the README's insistence on a case-sensitive
collation is the reasoning behind the planned case-only dead-predicate grade,
reached independently.

## What the DSL can say today, against what tuva-core needs to say

The proxy surface as built (`contracts/proxy.py`, `types/contract.py`), read
against the five test types.

| tuva assertion | DSL today | gap |
|---|---|---|
| composite primary key `(claim_id, claim_line_number, data_source)` | `self.key(...)`, `PrimaryKey` marker | none |
| `(person_id, data_source)` references patient | `references` and `ForeignKey` are single-column | composite edge |
| `(person_id, patient_id, data_source)` triple present in patient | as above | composite edge |
| `(claim_id, data_source)` determines each header column | `determines` takes one determinant at the proxy; `DeterminesFact` already carries a tuple | tuple determinant at the proxy |
| one value per claim, where a NULL among non-nulls counts as a second value | `determines` has no NULL rule | the rule must be part of the fact |
| `person_id` not null; `claim_start_date` not null | no fact form; `is_not_null()` builds a run-only predicate | a `not_null` fact |
| `bill_type_code` not null where the claim is institutional | no `where` on the proxy | scope, and a conditional not-null fact |
| `death_flag = 1` implies `death_date` not null | none | the same conditional not-null fact |
| `sex in ('male','female','unknown')` | `NominalEnum` bound to a column is recognized then inert (#135) | value domain from an enum (#135, #36) |
| `race` found in `terminology__race.description`, case-insensitively | a referential edge in all but name | `references` with a comparison rule |
| `paid_amount >= 0` | `Field(ge=0)` is captured as a `ColumnConstraint` and not yet run | the guard emitter |
| `paid_date >= claim_end_date`; `start <= end` | `Compare` predicate, run-only | the guard emitter |
| overlapping enrollment spans | window functions are outside the proxy vocabulary | keep as SQL; the frame escape hatch |
| `year_month` matches `YYYYMM` | string functions outside the proxy | keep as SQL |
| severity 1 to 3 per test | severity is per finding kind | a guard-severity attribute, if guards emit dbt tests |

Read down the gap column and the shape is clear. Everything on the fact side
is a small generalization of a constructor that exists: a tuple where a
column is accepted, a NULL rule on an existing fact, a scope modifier the
design already documents, and one new fact for nullability that the substrate
already propagates (a `where`-filtered dbt `not_null` test is consumed today
by the conditional activation machinery in `properties/nullability.py`, so
the DSL form has a consumer waiting). Everything on the predicate side is
either captured and unrun, or rightly left to SQL.

## Does declaring these facts help the analysis as it stands?

Two experiments on the tuva-core integration manifest (309 models scanned),
with the untracked `dblect/` contracts already present in that checkout.
Baseline: 124 findings, all structural.

| findings | count |
|---|---|
| join_fanout (warn) | 70 |
| join_on_nullable_key (error) | 27 |
| non_unique_window_order_keys (error) | 20 |
| null_group_on_nullable_key (error) | 4 |
| coalesce_on_join_key (error) | 3 |

**Keys.** Declaring all fifteen Input Layer primary keys from the YAML
`is_primary_key` metadata, as `self.key(...)` contracts, discharged one
finding: the fan-out at the `normalized__medical_claim` join to the
claim-line date-normalization model on `(claim_id, claim_line_number,
data_source)`. Adding a key on `provider_data__provider.npi` changed nothing
further. The window finding on `normalized_input__int_medical_npi_normalize`
(`ROW_NUMBER() OVER (PARTITION BY data_source, claim_id ORDER BY
claim_line_number)`) survived both runs even though the declared key covers
the partition and order columns exactly. The path is a `SELECT DISTINCT` over
three left joins to the provider seed; whether the key is lost at the
`DISTINCT`, at the joins, or at the `select_extension_columns` macro in the
staging model is a propagation question, and this model pair is the fixture
to answer it with. That is the first concrete return from declaring input
facts: they turn "the analysis did not see a key" into a reproducible case.

**Nullability.** Tuva asserts `claim_start_date` and `claim_line_start_date`
are never null, at its highest severity. Thirteen `join_on_nullable_key`
errors fire on `start_date` in the encounter anchor-matching models, and
`start_date` is `coalesce(admission_date, claim_line_start_date,
claim_start_date)`. A not-null fact on the input column would still not
discharge them, because `normalized__medical_claim` rebuilds every date as a
`coalesce` over columns from left-joined aggregation models. The nullability
is re-introduced structurally, by design, one layer down. Two declarations
would speak to it, and both are extensions:

- A declared *total* edge from a claim into its own per-claim aggregation
  (every claim has a row there by construction, the same shape the
  referential plan names for `encounters__patient_data_source_id`). Given a
  total edge, the nullability property can treat a `LEFT JOIN` across it as
  preserving the parent column's own nullability rather than padding it.
- A declared not-null on `normalized__medical_claim.claim_start_date`, which
  the analysis would grade today as not established, and which would surface
  as one precise finding at the source instead of thirteen consequences.

**Conditional nullability.** Ten `join_on_nullable_key` errors fire on
`bill_type_code`, `place_of_service_code`, `revenue_center_code`,
`facility_npi`, and `rendering_npi` at left joins to terminology lookups.
Tuva's own tests say these columns are null exactly when the claim type
makes them inapplicable, so the NULL non-match is the intended behavior. A
conditional not-null fact would not silence the finding on its own, but it
lets the message say where the nulls live, and it lets a downstream
`where claim_type = 'institutional'` activate the unconditional fact through
the machinery that already does this for `where`-filtered dbt tests.

**Functional dependencies.** Not tested, because the proxy accepts a single
determinant and every tuva dependency is scoped by `data_source`. Their
payoff is at aggregations that group by claim and select header columns; the
remaining fan-outs in `normalized__medical_claim` are about the voting models'
`ROW_NUMBER() = 1` filters, which the row-number substrate work (#216) owns,
not an input fact.

So: yes, extend the untracked declarations with the input keys. They are
tuva-core's own stated contract, they are cheap, and they are true. Expect a
modest immediate discharge and a larger deferred one, since the referential
orphan-drop check, the dead-predicate check, and the fixture generator all
read exactly these facts once built. Hold the FDs and not-null facts until the
proxy can spell them.

## Extensions, in the order they pay

1. **A `not_null` fact with scope.** One new fact node, lowered to the
   `Nullability` property the substrate already carries; `self.where(pred)`
   as the scope modifier the design documents, producing the same conditional
   fact a `where`-filtered dbt `not_null` produces today. Suggested spelling:
   `self.bill_type_code.not_null().where(self.claim_type == 'institutional')`.
   This covers 77 tuva "missing" tests and the implication-shaped
   "consistency" tests in one construct.
2. **Tuple determinants and a NULL rule on `determines`.** The AST already
   holds a tuple; only the proxy narrows it. The NULL rule should be an
   explicit enum on the fact (NULL is a value; NULL is ignored), because
   tuva ships both readings and a generated guard has to know which to emit.
3. **Composite `references`.** Every key in a multi-source package is scoped
   by a source column, and single-column edges under-describe every join in
   tuva-core. This is the composite-key open question in the referential
   plan, answered: it is required, not optional.
4. **A totality facet on `references`.** A total edge is what lets the
   nullability property see through a `LEFT JOIN` to a model's own
   aggregation, and it is the same fact the orphan-drop check wants in order
   to say "no such rows are expected."
5. **Value domains from `NominalEnum`** (#135, #36), already planned. Note
   that a terminology-backed set is a referential edge, not a literal set,
   and keep it on `references`, with a comparison rule (case-insensitive,
   dots stripped) if the guard is to match tuva's lookups.

## What the runtime guard should look like

When the guard emitter is built, tuva's flag layer is a worked answer to the
questions it will face. Borrow: tri-state results with applicability separate
from failure; a grain and key tuple on every guard, taken from the fact's
relation rather than the declaring model; a stable name, description, and
catalog derived from the declaration, which removes the drift tuva has to
manage between SQL, description string, and test type; tested, passed,
failed, and not-applicable counts per source; and an optional failure-key
drilldown with a reversible tuple encoding.

Two things not to borrow. Every tuva guard is hand-written SQL, which is the
work an emitter exists to do. And tuva's layer never fails a build, the right
choice for observability over customer inputs and the wrong one for a guard
whose purpose is to make a vouched fact fail loudly. The same applicability
and failure pair can emit both a flag column for a dashboard and a dbt test at
error severity for the gate, and only the second satisfies the "loud failure
mode" criterion the orphan-drop plan already relies on.

## Open questions

- Where does the input key get lost between `input_layer__medical_claim` and
  the window in `normalized_input__int_medical_npi_normalize`? Bisect with a
  contract on `normalized_input__stg_medical_claim` and one on the `base`
  CTE's shape.
- Should a `LEFT JOIN` to a relation with a declared single-column key, on a
  nullable code column, grade quieter than the general nullable-key join? The
  ten terminology-lookup findings are the calibration set.
- Guard severity: per fact, or per fact kind? Tuva grades per test. If guards
  emit dbt tests, the natural home is a `severity` argument on the contract
  decorator, defaulting to error.
- Whether a project-facing YAML convention for keys (tuva writes
  `meta.is_primary_key`) is worth a generic reader under a dblect-owned
  `meta` namespace, or whether that stays in the deferred YAML-ergonomics
  bucket in the technical intro.
