# Real-project calibration: false positives and severity defaults

This is the corpus-labelling pass that [issue #125](https://github.com/dvryaboy/dblect/issues/125) asks for and that
[hazard-algebra.md](../design/hazard-algebra.md) names as the way severity cut points get set: run `dblect check`
against real dbt projects, label a sample of the findings by hand, and let the evidence pick which detectors are quiet
enough to fail a build by default and which should ship advisory. The fixtures the detectors are developed against are
small and constructed; this pass reports what the same detectors do on SQL nobody wrote for them.

## What the corpus is

Four public dbt projects on the DuckDB adapter, spanning a toy star schema, a retail mart, a large simulation pipeline,
and the project from the [dbt-duckdb blog post](https://duckdb.org/2025/04/04/dbt-duckdb). Every number below is
reproducible from a checked-out copy of each project plus its compiled manifest.

| Project | Source | Adapter |
| --- | --- | --- |
| `jaffle_shop_duckdb` | the canonical dbt demo | duckdb |
| `retail_analytics` | a retail marts project | duckdb |
| `nba-monte-carlo` | sports-simulation pipeline (`sports_sims`) | duckdb |
| `dutch_railway_network` | the dbt-duckdb blog project | duckdb |

GitLab's internal analytics project (Snowflake) was the intended fifth entry for a second dialect and a much larger
model count. Its repository now requires authentication for a clone, so it is deferred to a run with credentials; the
Snowflake dialect and the robustness angle it would exercise stay open.

Reproduction, from each project root with a compiled `target/manifest.json`:

```
dblect check <project_dir> --format json
```

`dutch_railway_network` compiles against a local-only DuckDB profile (the blog project's live profile attaches a remote
DuckDB blob and a Postgres instance that a compile does not need); `dbt compile` renders the SQL into the manifest
without those attachments. `nba-monte-carlo` is analysed against the manifest under its `docs/` directory.

## Results

| Project | Models analysed | Findings | Unbuilt | Resolution coverage |
| --- | ---: | ---: | ---: | ---: |
| `jaffle_shop_duckdb` | 5 | 1 | 0 | 100.0% |
| `retail_analytics` | 9 | 1 | 0 | 100.0% |
| `nba-monte-carlo` | 63 | 3 | 4 | 94.7% (24 blind, 3 unexpanded `SELECT *`) |
| `dutch_railway_network` | 11 | 5 | 0 | 71.4% (18 blind) |
| **Total** | **88** | **10** | **4** | |

Findings by detector, over the whole corpus:

| Detector | Count | Default severity |
| --- | ---: | --- |
| `join_fanout` | 5 | error |
| `non_deterministic_function` | 2 | warn |
| `null_group_after_outer_join` | 1 | error |
| `non_unique_window_order_keys` | 1 | error |
| `where_on_outer_joined_nullable` | 1 | error |

Only the structural family fired: none of these projects declares dblect contracts, so the declaration family
(`domain_type_contradiction`, `aggregation_not_well_typed`, `join_key_type_mismatch`, `contract_issue`) had nothing to
resolve. This pass calibrates the structural detectors; the declaration family needs a contract-carrying corpus, which
the scenario fixtures supply and a real adopter project will supply later.

## Hand classification

Each finding was read against its model. The categories are:

- **Real hazard**: the pattern would surprise the author; the result can be wrong or drift between runs.
- **Sound, likely intended**: the detector is correct about the SQL, and the author most likely wants the pattern. The
  `-- noqa` path or a declared contract is the intended quieting mechanism.
- **Logic false positive**: the condition the detector claims does not actually hold.

The headline result is that **the corpus produced zero logic false positives**. Every one of the ten findings is a true
statement about the SQL. The noise that exists is intent-noise: sound findings on patterns the author intended. That
matters for what the calibration lever is. The lever is severity posture and grounding coverage, not detector logic.

| # | Project | Detector | Class | Note |
| --- | --- | --- | --- | --- |
| 1 | jaffle | `null_group_after_outer_join` | Real hazard | `sum(amount) ... left join orders ... group by orders.customer_id`; payments with no matching order collapse into one NULL-`customer_id` group whose sum mixes unrelated payments. |
| 2 | retail | `non_unique_window_order_keys` | Real hazard | `row_number() over (partition by user_id order by product_count desc)`; `product_count` is a `count`, so ties are expected and the top-department pick is non-deterministic on a tie. |
| 3 | nba | `where_on_outer_joined_nullable` | Sound, likely intended | a `full outer join` to a schedule seed, then `where a.date <= start_date`; `a` is nullable after the full join, so the predicate drops seed-only (unplayed) rows and inverts the outer join. For a `raw_results` table that is plausibly the intent; the fix or a `-- noqa` records it. |
| 4-5 | nba | `non_deterministic_function` | Sound, intended | `RAND()` in a window `ORDER BY` inside a Monte Carlo simulation. Randomness is the model's purpose. Already advisory (`warn`). |
| 6-10 | dutch_railway | `join_fanout` | Sound, intended-unique key | five fact-to-dimension joins on surrogate keys (`station_sk`, `municipality_sk`, `province_sk`). |

## The join_fanout case, in detail

Five of the eight error-level findings are one detector on one project, and they are the most important signal in the
pass, because the pattern they flag is the single most common join in dimensional modelling.

`dim_nl_train_stations` builds its surrogate key as `generate_surrogate_key(['station_code'])`, a deterministic function
of `station_code`, which carries a dbt `unique` test. The fact models then join to the dimension on `station_sk`. dblect
knows the dimension's declared keys (it reports `known: (station_id); (station_code)`), so it has the natural key's
uniqueness in hand. What it does not do is carry that uniqueness across the surrogate-key expression: a deterministic
function of a unique key column-set is itself a uniqueness key, and dblect does not yet ground `station_sk` from
`station_code`. So it cannot cover the column the join actually uses, and it fires, soundly, because it cannot prove the
join is safe.

At the current `error` default this means a textbook star schema fails the build on every fact-to-dimension join. That is
the exact failure mode #125 exists to catch: one detector firing on a ubiquitous, intended pattern is what poisons trust
in the whole set.

## Determination

**1. The detectors are sound on real SQL.** Zero logic false positives across 88 models. The broad-net,
false-positive-tolerant posture the structural layer documents holds up: what fires is correct, and the design's
reliance on `-- noqa` and contracts to quiet intended patterns is the right shape. The calibration work is about
defaults, not about detector correctness.

**2. `join_fanout` (and `cross_model_fanout` with it) should not fail a build by default until it grounds
surrogate-key uniqueness.** Two ways to get there, and both are worth doing:

- *Grounding, the real fix.* Propagate uniqueness through a surrogate-key expression: a deterministic function over a
  declared or inferred unique key column-set is a uniqueness key. This grounds `station_sk` from `station_code`'s test
  and clears all five findings with no loss of soundness, since the surrogate key inherits the natural key's uniqueness.
  This extends the determines-grounding already landed for `join_fanout` rather than starting a parallel mechanism.
- *Severity posture, the interim default (done in this pass).* Until the grounding lands, `join_fanout` at `error` is
  too aggressive for the default profile, so `join_fanout` and `cross_model_fanout` now default to `warn`
  (`src/dblect/severity.py`), keeping them visible without failing CI on an undeclared-but-intended key. The `-- noqa`
  and contract quieting paths are unchanged. **This is a temporary demotion.** When the lenient/strict split (#116)
  lands it must raise the fanout pair back to `error` in the strict profile (and the grounding fix above lets the
  default profile hold `error` too), so the demotion does not quietly become the permanent default.

**3. `non_deterministic_function` at `warn` is confirmed.** The Monte Carlo case is the textbook reason this detector is
advisory: the randomness is load-bearing and intended, and a `warn` states the hazard without failing the run.

**4. The three single-fire error detectors stay at `error`.** `null_group_after_outer_join`,
`non_unique_window_order_keys`, and `where_on_outer_joined_nullable` each fired once, each soundly. Two are real hazards;
`where_on_outer_joined_nullable`'s `raw_results` case is the intended-exception that the `-- noqa` path is built to
carry, and it shows the suppression ergonomics matter for the error-level detectors.

## Coverage and robustness

Resolution coverage is full on the two small projects and stays high on the large one (94.7% on 63 models). The two
places it drops are informative and neither is a parse defect:

- **`dutch_railway_network` at 71.4%** is source-column blindness. The project reads from external DuckDB sources, and no
  `catalog.json` was generated for the run, so undocumented source leaves do not resolve. `dbt docs generate` (or
  `--catalog`) would lift this. Zero models were unbuilt: every SQL model parsed.
- **`nba-monte-carlo`'s 3 unexpanded `SELECT *`** are the wildcard-relation shape that drops resolution, feeding the
  wildcard/computed-relation robustness work (#87).

No SQL model failed to parse anywhere in the corpus. The four `nba-monte-carlo` models reported as unbuilt
(`nba_elo_rollforward`, `nba_tiebreakers_optimized`, and their NFL twins) are dbt **Python** models (`.py`), which the
SQL analyser has no compiled SQL for. They are reported with the reason `model has no parsed SQL`, which reads the same
as a genuine parse failure. Distinguishing "Python model, out of the SQL analyser's scope" from "SQL that failed to
parse" is tracked in #138 (the same distinction the aggregation checker needs between a skipped Python model and a real
conflict); this pass is a second place that distinction pays off.

## Follow-ups this pass opens

- Uniqueness propagation through surrogate-key expressions, which grounds `join_fanout` on star schemas (extends #197).
- The lenient/strict severity split (#116): `join_fanout` is the first concrete detector the split should separate, and
  this pass is the evidence for where its cut point sits. **#116 must raise the fanout pair back to `error` in the strict
  profile**, undoing the interim `warn` demotion this pass applied.
- Distinguish Python models from parse failures in the unbuilt report (#138).
- A Snowflake corpus entry (GitLab analytics or similar) once credentials are available, for the second dialect and the
  robustness shapes a larger project exercises.
