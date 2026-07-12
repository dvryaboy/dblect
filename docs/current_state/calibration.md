# Real-project calibration: false positives and severity defaults

This is the corpus-labelling pass that [issue #125](https://github.com/dvryaboy/dblect/issues/125) asks for and that
[hazard-algebra.md](../design/hazard-algebra.md) names as the way severity cut points get set: run `dblect check`
against real dbt projects, label every finding by hand, and let the evidence pick which detectors are quiet enough to
fail a build by default, which should ship advisory, and which carry a precision gap worth fixing. The fixtures the
detectors are developed against are small and constructed; this pass reports what the same detectors do on SQL nobody
wrote for them, across three warehouse dialects.

The precision follow-ups this pass first opened have since landed, and the numbers below are the tool as it stands after
them: the flatten and `WHERE` idiom fixes ([#210](https://github.com/dvryaboy/dblect/pull/210)), the per-site finding
collapse ([#211](https://github.com/dvryaboy/dblect/pull/211)), and the Spark struct-field lineage fix
([#212](https://github.com/dvryaboy/dblect/pull/212)), all against the first published release
([#209](https://github.com/dvryaboy/dblect/pull/209), dblect 0.1.0).

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

Four of the six projects were re-run after the precision fixes (the two DuckDB projects that were checked out,
`snowflake_spend`, and `wikimedia`). `nba-monte-carlo` and `dutch_railway`, both DuckDB and neither exercising the
Spark and per-site paths the fixes touch, carry the prior pass's figures and are marked accordingly.

| Project | Dialect | Models | Findings | Unbuilt | Resolution |
| --- | --- | ---: | ---: | ---: | ---: |
| `jaffle_shop` | duckdb | 5 | 1 | 0 | 100% |
| `retail_analytics` | duckdb | 9 | 1 | 0 | 100% |
| `nba-monte-carlo` † | duckdb | 63 | 3 | 4 | 95% |
| `dutch_railway` † | duckdb | 11 | 5 | 0 | 71% |
| `snowflake_spend` | snowflake | 4 | 0 | 1 | 84% |
| `wikimedia/dbt-jobs` | spark | 80 | 20 | 0 | 76% |

† prior-pass figures, not re-run in this update.

Wikimedia is where every fix bites, and the movement is all there: **37 findings to 20, 11 unbuilt models to 0,
resolution 73% to 76%.** The other five projects are unchanged. Only the structural family fired anywhere: none of these
projects declares dblect contracts, so the declaration family (`domain_type_contradiction`, `aggregation_not_well_typed`,
`join_key_type_mismatch`, `contract_issue`) had nothing to resolve. This pass calibrates the structural detectors; the
declaration family needs a contract-carrying corpus, which the scenario fixtures supply and a real adopter project will
supply later.

## Classification

Every finding was read against its model and labelled a **true positive** (a real hazard the author most likely did not
intend), **intended** (the detector is correct about the SQL and the author wants the pattern, quieted through `-- noqa`
or a declared contract), or a **false positive** (the condition the detector claims does not actually hold).

Wikimedia carries the only non-trivial finding set, so it is the one worth reading in full. Its 20 findings:

| Detector | Findings (was) | Read | Default severity |
| --- | ---: | --- | --- |
| `null_group_after_outer_join` | 5 (18) | one clean true positive; the rest sit in the open guarded-grouping / derived-side gap | error |
| `join_fanout` | 8 (13) | intended surrogate-key joins plus the one real fan-out; ships advisory | warn (this pass) |
| `inner_flatten_row_drop` | 5 (9) | all correct fires now; the literal-grid false positives cleared | error |
| `where_on_outer_joined_nullable` | 1 (3) | a genuine LEFT JOIN inverted by a nullable-side `WHERE`; the `COALESCE(pred, TRUE)` false positive cleared | error |
| `unordered_aggregate` | 1 (1) | correct (`ARRAY_AGG` with no `ORDER BY`) | warn |

The other five projects add one finding each of note: `jaffle_shop`'s single `null_group`, and `retail_analytics`'s
single `non_unique_window_order_keys`, both true positives at `error`; `snowflake_spend` fires nothing. The DuckDB
corpus that was re-run carries no false positives.

The false-positive idioms the first pass measured are gone. `inner_flatten_row_drop` is now all correct fires: the four
column-array explodes in `signal_driven_handling_bad_faith_accounts` (`EXPLODE(r.users_in_case)`, which can genuinely be
empty) and one `GENERATE_SERIES` spine with non-literal bounds in `active_moderators_monthly` that the detector rightly
cannot prove non-empty. The literal category-grid explodes that used to fire now clear. `where_on_outer_joined_nullable`
no longer flags the `COALESCE(pred, TRUE)` self-revert guard. And `null_group_after_outer_join` emits one finding per
`GROUP BY` clause rather than one per column, so the `base_moderator_actions_logging` grouping over nine columns reads as
a single finding instead of a burst.

### The one remaining precision gap

`null_group_after_outer_join` is where the residual noise concentrates, and it is the deeper gap the first pass already
named. Of Wikimedia's five, one is the clean true positive (`base_moderator_actions_logging`, grouping by the nullable
`event_user_id` with no co-grouped identity key). The rest are over-fires a lineage-aware guard would clear: a co-grouped
identity key that pins the bucket (`base_moderator_actions_checkuser` groups by `cul_user_text` alongside the nullable
`event_user_id`), an effectively-preserved identity side of a derived-side join, and groupings on boolean flags whose
unmatched bucket is meaningful. Distinguishing these needs the grouped key's lineage and functional dependencies, not a
structural read alone, so it is the substantive follow-up this detector still carries.

## Determination

**1. The detectors are sound in their core judgement, and the residual noise is bounded and diagnosable.** The
re-run DuckDB corpus carries no false positives, and Spark's earlier noise traced to specific, fixable causes rather than
a detector reasoning about SQL incorrectly. The idiom-level and multiplicity causes are now fixed; what remains is the
one `null_group` guarded-grouping gap above. The broad-net, false-positive-tolerant posture the structural layer
documents holds, and the `-- noqa` and contract quieting paths carry the intended cases.

**2. `join_fanout` (and `cross_model_fanout`) default to `warn` (done in this pass).** It is the highest-volume detector
after `null_group` and its findings are dominated by two safe idioms: fact-to-dimension joins on a surrogate key that is
a function of a declared-unique natural key (`dutch_railway`), and joins on a subset of a composite key whose missing
column is functionally determined by the join columns through a canonical primary key (`wikimedia`). At `error` that
fails CI on the most common join in dimensional modelling. It stays valuable at `warn`: it caught the one real fan-out in
the corpus, `retained_newcomers` joining on an unstable `user_name` that does not determine `reg_ts`. **This is a
temporary demotion.** The real fix is to ground uniqueness through the surrogate-key expression and through the canonical
FD; when the lenient/strict split ([#116](https://github.com/dvryaboy/dblect/issues/116)) lands it must raise the
fan-out pair back to `error` in the strict profile, so the interim `warn` does not become the permanent default.

**3. The idiom precision fixes landed.** `inner_flatten_row_drop` now clears a provably non-empty array in every flatten
spelling, and `where_on_outer_joined_nullable` recognises the `COALESCE(pred, TRUE)` guard
([#210](https://github.com/dvryaboy/dblect/pull/210)), so both detectors can be relied on at `error` without a known
false-positive idiom.

**4. Per-site emission landed.** `null_group_after_outer_join` reports one finding per `GROUP BY` clause
([#211](https://github.com/dvryaboy/dblect/pull/211)), so a single grouping decision is one finding. `inner_flatten`
stays one finding per lateral, since each arm is a distinct location with its own inner-vs-outer fix.

**5. The already-advisory detectors are confirmed.** `non_deterministic_function` at `warn` is confirmed by the
`nba-monte-carlo` `RAND()`-in-a-simulation case; `unordered_aggregate` at `warn` by Wikimedia's `ARRAY_AGG` with no
`ORDER BY`. The single-fire `error` detectors that were clean (`null_group` at jaffle, `non_unique_window_order_keys` at
retail) stay at `error`.

## Coverage and robustness

Resolution coverage is full on the small DuckDB projects and holds up at scale (95% on 63 models). The Spark
struct-field lineage gap is closed, and the remaining drops are informative source-column blindness, none a parse defect
on a SQL model:

- **`wikimedia` at 76%, with 0 unbuilt models (was 73% and 11).** Every one of the eleven previously-unbuilt models used
  the Spark `INLINE(ARRAY(STRUCT('mobile app' AS platform), ...))` category-grid idiom, whose struct fields sqlglot's
  qualify collapses to a single `_col_0`, so downstream references to `platform` / `project_family` fell blind or raised
  `Unknown column`. Expanding the generator to its named field columns before qualify
  ([#212](https://github.com/dvryaboy/dblect/pull/212)) resolves them, and the eleven now build.
- **`dutch_railway` at 71%** is source-column blindness: the project reads external DuckDB sources and no `catalog.json`
  was generated for the run, so undocumented source leaves do not resolve. `dbt docs generate` (or `--catalog`) lifts it.
  Zero models were unbuilt.
- **`snowflake_spend` at 84%** is `SELECT *` off sources with no catalog, the same source-leaf blindness.
- **`nba-monte-carlo`'s 4 unbuilt models** are dbt **Python** models (`.py`), reported as `model has no parsed SQL`, which
  reads the same as a genuine parse failure. Distinguishing "Python model, out of the SQL analyser's scope" from "SQL
  that failed to parse" is tracked in [#138](https://github.com/dvryaboy/dblect/issues/138).

No SQL model failed to parse anywhere in the corpus, across all three dialects. Snowflake (`DATE_TRUNC`, `DATEDIFF`,
`::` casts) and Spark (`LATERAL VIEW`, `EXPLODE`, `SEQUENCE`, `INLINE`) SQL parsed under the forced `--dialect`.

## Follow-ups still open

- Ground `null_group_after_outer_join` through the grouped key's lineage and functional dependencies, so a co-grouped
  identity key, a derived-side join, and a meaningful-bucket grouping clear rather than over-fire (the residual gap above).
- Ground `join_fanout` uniqueness through the surrogate-key expression and the canonical functional dependency; extends
  [#197](https://github.com/dvryaboy/dblect/pull/197). The lenient/strict split
  ([#116](https://github.com/dvryaboy/dblect/issues/116)) then raises the fan-out pair back to `error` in the strict
  profile, undoing the interim `warn` this pass applied.
- Distinguish Python models from parse failures in the unbuilt report ([#138](https://github.com/dvryaboy/dblect/issues/138)).
- A Snowflake corpus entry with real scale (GitLab `analytics` or similar) once credentials are available.

Landed since this pass opened: the flatten and `WHERE` idiom fixes
([#210](https://github.com/dvryaboy/dblect/pull/210)), the per-site finding collapse
([#211](https://github.com/dvryaboy/dblect/pull/211)), and the Spark struct-field lineage fix
([#212](https://github.com/dvryaboy/dblect/pull/212)).
