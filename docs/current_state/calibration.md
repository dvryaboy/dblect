# Real-project calibration: false positives and severity defaults

This is the corpus-labelling pass that [issue #125](https://github.com/dvryaboy/dblect/issues/125) asks for and that
[hazard-algebra.md](../design/hazard-algebra.md) names as the way severity cut points get set: run `dblect check`
against real dbt projects, label every finding by hand, and let the evidence pick which detectors are quiet enough to
fail a build by default, which should ship advisory, and which carry a precision gap worth fixing. The fixtures the
detectors are developed against are small and constructed; this pass reports what the same detectors do on SQL nobody
wrote for them, across three warehouse dialects.

## The corpus

Six public dbt projects, 172 analysed models, across DuckDB, Snowflake, and Spark.

| Project | Source | Dialect | Models |
| --- | --- | --- | ---: |
| `jaffle_shop_duckdb` | the canonical dbt demo | duckdb | 5 |
| `retail_analytics` | a retail marts project | duckdb | 9 |
| `nba-monte-carlo` | sports-simulation pipeline | duckdb | 63 |
| `dutch_railway_network` | the [dbt-duckdb blog](https://duckdb.org/2025/04/04/dbt-duckdb) project | duckdb | 11 |
| `gitlab-data/snowflake_spend` | GitLab's public Snowflake spend project | snowflake | 4 |
| `wikimedia/dbt-jobs` | Wikimedia's public analytics data-lake project | spark | 80 |

GitLab's internal `analytics` project (the large Snowflake one) is members-only, so it is deferred to a run with an
account that has access; `snowflake_spend` is the public Snowflake stand-in. Reproduction, from each project root with a
compiled `target/manifest.json`:

```
dblect check <project_dir> --format json            # duckdb projects, dialect read from the manifest
dblect check <project_dir> --dialect snowflake ...  # snowflake_spend
dblect check <project_dir> --dialect spark ...      # wikimedia
```

Two projects needed a manifest built for the run. `snowflake_spend` and `wikimedia/dbt-jobs` compile against an offline
DuckDB target (Wikimedia already ships one for its SQLFluff lint step), which renders the Jinja into compiled SQL
without a live warehouse; the compiled SQL keeps its native dialect and dblect parses it under the forced `--dialect`.
`snowflake_spend` has one model (`snowflake_amortized_rates`) that runs an introspective `date_spine` macro at compile
time and needs a real warehouse, so it is excluded from that project's run, and a small absent seed
(`snowflake_contract_rates`) is stubbed so the rest compiles.

## Results

| Project | Dialect | Models | Findings | Unbuilt | Resolution |
| --- | --- | ---: | ---: | ---: | ---: |
| `jaffle_shop` | duckdb | 5 | 1 | 0 | 100% |
| `retail_analytics` | duckdb | 9 | 1 | 0 | 100% |
| `nba-monte-carlo` | duckdb | 63 | 3 | 4 | 95% |
| `dutch_railway` | duckdb | 11 | 5 | 0 | 71% |
| `snowflake_spend` | snowflake | 4 | 0 | 1 | 84% |
| `wikimedia/dbt-jobs` | spark | 80 | 37 | 11 | 73% |
| **Total** | | **172** | **47** | **16** | |

Only the structural family fired: none of these projects declares dblect contracts, so the declaration family
(`domain_type_contradiction`, `aggregation_not_well_typed`, `join_key_type_mismatch`, `contract_issue`) had nothing to
resolve. This pass calibrates the structural detectors; the declaration family needs a contract-carrying corpus, which
the scenario fixtures supply and a real adopter project will supply later.

## Classification

Every finding was read against its model and labelled:

- **True positive**: a real hazard the author most likely did not intend.
- **Intended**: the detector is correct about the SQL, and the author wants the pattern (an intentional outer join whose
  null group is meaningful or impossible, a fan-out that is safe through a functional dependency dblect cannot see). The
  `-- noqa` path or a declared contract is the intended quieting mechanism.
- **False positive**: the condition the detector claims does not actually hold, so the finding is wrong.

| Detector | Total | True positive | Intended | False positive | Default severity |
| --- | ---: | ---: | ---: | ---: | --- |
| `null_group_after_outer_join` | 18 | 2 | 7 | 9 | error |
| `join_fanout` | 13 | 1 | 12 | 0 | warn (this pass) |
| `inner_flatten_row_drop` | 9 | 4 | 0 | 5 | error |
| `where_on_outer_joined_nullable` | 3 | 0 | 2 | 1 | error |
| `non_deterministic_function` | 2 | 0 | 2 | 0 | warn |
| `non_unique_window_order_keys` | 1 | 1 | 0 | 0 | error |
| `unordered_aggregate` | 1 | 1 | 0 | 0 | warn |
| **Total** | **47** | **9** | **23** | **15** |

The 15 false positives are not spread evenly. They concentrate in three specific, fixable places, and finding
multiplicity makes them look larger than the number of underlying mistakes.

### Finding multiplicity inflates the count

`null_group_after_outer_join` emits one finding per grouped column, and `inner_flatten_row_drop` one per flatten arm. So
a single grouping decision or a single cross-tab model produces a burst of findings. The 9 `null_group` false positives
are all **one** `GROUP BY` site (`base_moderator_actions_logging`), and the 5 `inner_flatten` false positives are **one**
model (`active_moderators_monthly`). Measured by distinct decision site rather than by column, the false-positive picture
is five underlying mistakes, not fifteen. Collapsing `null_group` to one finding per `GROUP BY` (and `inner_flatten` per
lateral) would report the same hazards with far less apparent noise, and is the first calibration recommendation.

### The three precision gaps

**1. `inner_flatten_row_drop` does not clear a provably non-empty array in the `explode`/`flatten` spellings.**
`active_moderators_monthly` builds a dense category grid with the standard idiom
`LATERAL VIEW EXPLODE(ARRAY('mobile', 'other'))` and `LATERAL VIEW EXPLODE(SEQUENCE(...))`. A literal array with constant
elements, and a bounded sequence, cannot be empty or NULL, so the flatten drops no row. dblect already knows this: it has
`array_literal_nonempty` and `generator_provably_nonempty` in `sql/vocab.py`, and they silence the equivalent
`UNNEST(ARRAY[...])`. The guard is not reached for Spark `explode` or Snowflake `flatten`: `_unnest_arg_provably_nonempty`
in `sql/patterns.py` returns early for anything that is not `exp.Unnest`, and the Spark `LATERAL VIEW` branch runs no
non-emptiness check at all (only the outer-form check). So the same provably-safe array reads as safe under `UNNEST` and
as a hazard under `explode`. The four true positives in `signal_driven_handling_bad_faith_accounts`
(`EXPLODE(r.users_in_case)`) are genuine, because a column array can be empty; the fix is to run the existing
non-emptiness vocabulary on the `explode`/`flatten` argument, not to weaken the detector.

**2. `where_on_outer_joined_nullable` does not recognise a `COALESCE(<predicate>, TRUE)` null-guard.**
`base_moderator_actions_reverts` excludes self-reverts with `COALESCE(reverted.event_user_text != r.event_user_text,
TRUE)`. When the outer join misses, the inner comparison is NULL and the `COALESCE` returns TRUE, so the unmatched row is
kept, which is exactly the inversion the detector warns about being avoided. The detector reads the bare inner comparison
and misses the enclosing `COALESCE` default. Recognising `COALESCE(pred, TRUE)` as a guard (the dual of the
`COALESCE(pred, FALSE)` and `IS NULL` guards the nullability work already catalogs) clears it.

**3. `null_group_after_outer_join` over-fires on a FULL JOIN whose sides share lineage, and on guarded groupings.**
The nine false positives are one `FULL JOIN` where the right relation is built entirely from the left
(`user_status_monthly` derives from `get_actor_names`), so the flagged `a.*` columns are the effectively-preserved
identity side and never collapse for a real actor. The intended cases nearby carry a visible guard the detector could
read: a co-grouped identity key (`base_moderator_actions_checkuser` groups by `cul_user_text` alongside the nullable
`event_user_id`, so the NULL bucket cannot mix unrelated users) or a `COALESCE(ua.compliance_level, 'not_applicable')`
that turns the unmatched rows into a meaningful bucket. The clean separation is the one true positive,
`base_moderator_actions_logging:170`, which groups by the nullable `event_user_id` with no co-grouped identity key. This
gap is deeper than the first two: distinguishing a derived-side FULL JOIN needs lineage, and the co-grouped-key heuristic
needs care, so the near-term win here is the multiplicity fix above, which drops the nine to one.

## Determination

**1. The detectors are sound in their core judgement, and the noise is bounded and diagnosable.** On the DuckDB corpus
(88 models) there were no false positives at all. Scaling to Spark surfaced fifteen, and every one traces to a specific,
fixable cause: three precision gaps and per-column finding multiplicity, not a detector reasoning about SQL incorrectly
at its core. The broad-net, false-positive-tolerant posture the structural layer documents holds, and the `-- noqa` and
contract quieting paths carry the intended cases.

**2. `join_fanout` (and `cross_model_fanout`) default to `warn` (done in this pass).** It is the highest-volume detector
after `null_group` and its findings are dominated by two safe idioms: fact-to-dimension joins on a surrogate key that is
a function of a declared-unique natural key (`dutch_railway`), and joins on a subset of a composite key whose missing
column is functionally determined by the join columns through a canonical primary key (`wikimedia`). At `error` that
fails CI on the most common join in dimensional modelling. It stays valuable at `warn`: it caught the one real fan-out in
the corpus, `retained_newcomers` joining on an unstable `user_name` that does not determine `reg_ts`. **This is a
temporary demotion.** The real fix is to ground uniqueness through the surrogate-key expression and through the canonical
FD; when the lenient/strict split (#116) lands it must raise the fan-out pair back to `error` in the strict profile, so
the interim `warn` does not become the permanent default.

**3. Two precision fixes are worth doing before relying on the `error` defaults of these detectors.** The
`inner_flatten_row_drop` explode/flatten non-emptiness gap (five false positives, `error` severity, the fix reuses
existing vocabulary) and the `where_on_outer_joined_nullable` `COALESCE(pred, TRUE)` guard (one false positive) are both
bounded and low-risk. Until they land, both detectors carry a known false-positive idiom at `error`.

**4. Collapse per-column emission to per-site.** `null_group_after_outer_join` and `inner_flatten_row_drop` should report
one finding per `GROUP BY` / per lateral. This alone turns the corpus's 15 false positives into 5 and makes the
false-positive rate legible.

**5. The already-advisory detectors are confirmed.** `non_deterministic_function` at `warn` is confirmed by the
`nba-monte-carlo` `RAND()`-in-a-simulation case; `unordered_aggregate` at `warn` by Wikimedia's `ARRAY_AGG` with no
`ORDER BY`. The single-fire `error` detectors that were clean (`null_group` at jaffle, `non_unique_window_order_keys` at
retail) stay at `error`.

## Coverage and robustness

Resolution coverage is full on the small DuckDB projects and holds up at scale (95% on 63 models). The places it drops
are informative, and none is a parse defect on a SQL model:

- **`wikimedia` at 73%, with 11 unbuilt models.** All eleven fail column-lineage qualification with `Unknown column:
  platform` / `Unknown column: project_family`, and all use the Spark `EXPLODE(ARRAY(STRUCT('mobile app' AS platform),
  ...))` idiom: the qualifier does not resolve a struct field introduced by an `explode` of an array of structs. The
  structural detectors still run on these models' ASTs (several of the findings above come from unbuilt models); it is
  the column lineage that goes blind. This is a concrete Spark lineage-robustness gap.
- **`dutch_railway` at 71%** is source-column blindness: the project reads external DuckDB sources and no `catalog.json`
  was generated for the run, so undocumented source leaves do not resolve. `dbt docs generate` (or `--catalog`) lifts it.
  Zero models were unbuilt.
- **`snowflake_spend` at 84%** is `SELECT *` off sources with no catalog, the same source-leaf blindness.
- **`nba-monte-carlo`'s 4 unbuilt models** are dbt **Python** models (`.py`), reported as `model has no parsed SQL`, which
  reads the same as a genuine parse failure. Distinguishing "Python model, out of the SQL analyser's scope" from "SQL
  that failed to parse" is tracked in #138.

No SQL model failed to parse anywhere in the corpus, across all three dialects. Snowflake (`DATE_TRUNC`, `DATEDIFF`,
`::` casts) and Spark (`LATERAL VIEW`, `EXPLODE`, `SEQUENCE`) SQL parsed under the forced `--dialect`.

## Follow-ups this pass opens

- Fix `inner_flatten_row_drop` to clear a provably non-empty array in the `explode`/`flatten` spellings, reusing
  `array_literal_nonempty` / `generator_provably_nonempty` (5 false positives).
- Fix `where_on_outer_joined_nullable` to recognise `COALESCE(pred, TRUE)` as a null-guard (1 false positive).
- Collapse `null_group_after_outer_join` and `inner_flatten_row_drop` to one finding per decision site.
- Ground `join_fanout` uniqueness through the surrogate-key expression and the canonical functional dependency; extends
  #197.
- The lenient/strict split (#116) must raise the fan-out pair back to `error` in the strict profile, undoing the interim
  `warn` this pass applied.
- Spark column-lineage for `EXPLODE(ARRAY(STRUCT(...)))` struct fields (11 Wikimedia models go lineage-blind).
- Distinguish Python models from parse failures in the unbuilt report (#138).
- A Snowflake corpus entry with real scale (GitLab `analytics` or similar) once credentials are available.
