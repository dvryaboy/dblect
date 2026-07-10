# Contracts that supply the intent the audit is guessing

*Status: working design notes. Five contract shapes where a declaration turns an ambiguous structural signal into a decision. Each rests on facts the substrate already carries; what each still needs is small and named at the end of its section, and it is a reader, an emitter, or in one case a facet in the type algebra. Where the lineage trace cannot derive the fact (an opaque UDF, a Python model), the declaration is the workaround. Growing the trace is a fine move where it is possible, and it is called out where it is not.*

## The shared premise

The zero-declaration audit and the contract layer read the same substrate. The audit guesses intent from structure and reports a hazard; its precision is bounded by the facts it can see. A contract supplies the intent directly, so the same computation has ground truth to compare against. The findings this produces come in two strengths, and the report keeps them distinct. Rarely, the declaration and a propagated fact are genuinely incompatible, a contradiction no data can reconcile (a domain-type conflict is the standing example). Far more often the analysis shows the construction does not *establish* the declaration and names the operator that defeats it (a join that can multiply, a union that merges two mappings), while the data may still satisfy it; that finding says "declared but not established here," never "violated." The verdict vocabulary behind this split lives in `refutation-and-verdicts.md`; each section below yields the second, weaker strength unless it says otherwise.

A declaration does two things, and they pull opposite ways on the alert count. It sharpens a check the audit already runs by supplying the intent the audit was guessing, which can silence a shape the author declared expected or confirm one it declared forbidden. It also opens conditions the audit cannot raise on its own, because they are wrong only relative to a stated intent. Conservation below is both. Its over-counting half refines the fan-out detectors the audit already runs (`detect_join_fanout`, `detect_cross_model_fanout`), and its under-counting half, a drop that no replication makes visible, is a bug only a declared intent can tell apart from ordinary filtering.

Each section gives SQL that triggers the problem, why the audit cannot settle it undeclared, the DSL that declares the intent, and how the declaration closes the gap. The DSL is the surface that exists today (`ModelContract`, `@contract`, column proxies, `ForeignKey`, `Money`/`Currency`); the wiring each contract still needs is named at the end of its section.

---

## 1. Conservation: a measure is neither created nor destroyed along a chain

**SQL that triggers it.** A revenue mart attributes each order's revenue across its touchpoints with a warehouse UDF, then sums per day:

```sql
-- models/marts/fct_attributed_revenue.sql
select
    o.order_date,
    sum(ml.attribute(o.order_id, t.touchpoint_id)) as attributed_revenue
from {{ ref('stg_orders') }} o
join {{ ref('stg_touchpoints') }} t on t.order_id = o.order_id
group by o.order_date
```

`ml.attribute` splits an order's revenue across its touchpoints. It takes ids and looks the revenue up internally, so `attributed_revenue` is `stg_orders.revenue` in meaning while its syntax names only `order_id` and `touchpoint_id`. The join fans each order across its touchpoints, and the day's sum is right only if the UDF's slices add back to the order's revenue. Conservation also breaks the other way: the inner join drops orders with no touchpoint, so a day whose orders all lack one produces no row at all, a hole that no wrong number makes visible.

**Why the audit alone cannot settle it.** With a bare magnitude the over-count is caught for free. `sum(o.revenue)` over this fan-out is flagged by `detect_join_fanout` the moment `stg_touchpoints`'s key is known, and that key is usually already harvested from the project's dbt `unique` tests (`unique_test_discoverer`, `unique_combination_discoverer`), lowered to the same `CandidateKeySet` fact a `PrimaryKey` or `grain(per=...)` declaration would supply. The UDF is what defeats this. `where_provenance` follows syntax: the column walk sees `order_id` and `touchpoint_id` inside `ml.attribute(...)`, so it traces `attributed_revenue` to those ids and never to `revenue`. The detector cannot tell this sum concerns the conserved measure, nor whether the UDF already apportioned it to the touchpoint grain. Whether the sum double counts turns on what the opaque UDF did, which the audit cannot see. It either stays silent (a miss) or flags every such sum (noise). It is guessing.

**DSL that declares the intent.**

```python
class FctAttributedRevenue(ModelContract):
    dbt_model = "marts.fct_attributed_revenue"
    attributed_revenue: Money.columns(amount="attributed_revenue", currency="currency")

    @contract
    def conserves_order_revenue(self):
        return (
            self.attributed_revenue.sum().group_by(self.order_date)
            == models.stg_orders.revenue.sum().group_by(models.stg_orders.order_date)
        ).within(0.01)
```

**How the predicate closes the gap.** The declaration states the link the UDF's syntax hid: `attributed_revenue` here is `stg_orders.revenue`, conserved per `order_date`. That is the origin `where_provenance` could not reach, so the analysis stops depending on the trace. It can now say what it could not before: the conserved measure fans out at the touchpoint join, and conservation survives only if `ml.attribute` apportions revenue to that grain. Where an apportionment is visible in SQL, the analysis discharges it. Where it lives inside the opaque UDF, the analysis does not fall silent; it localizes the obligation to that UDF, a precise "the conserved measure fans out here, and its apportionment is opaque at `ml.attribute`" finding, discharged by running the model or by annotating the UDF's effect (the opaque-reader opt-out, #42). The drop direction reads the same way: the declaration is what lets the inner join that empties a day be reported rather than waved through as ordinary filtering.

**What the analysis decides, and what it does not.** It decides the shape of the risk, not the fix. It shows the join can multiply the conserved measure with nothing renormalizing it afterward, or that the join can drop contributing rows, and it names the hop. Whether the multiplication or the drop lands on real rows is the data's call (an order with a single touchpoint multiplies nothing), so these are not-established findings that localize an unmet obligation rather than assert a wrong number. It can confirm one structural fix, a divisor equal to the fan-out degree (`count(*) over (partition by order_id)`), because that provably cancels the replication the fan-out fact measured. It cannot certify a data-dependent apportionment: that `ml.attribute`'s slices sum to the order's revenue is a property of the data the UDF returns, discharged by running or by a second declared conservation contract, not by static analysis.

**What we do with it.** The contract method compiles to a `ResolvedPredicate`, the AST `Compare(EQ, Agg(SUM, attributed_revenue by order_date), Agg(SUM, stg_orders.revenue by order_date), tol=0.01)`, carrying the measure, its declared origin, and the grain. Today that object is counted and dropped. Reading it means taking the two endpoints and the grain off the AST, walking the lineage between the origin model and this one, and classifying each hop against the facts already carried: a visible fan-out on a bare magnitude is a finding, a visible drop is a finding, and a hop that crosses an opaque scope (the UDF, a Python model, a wildcard ref #87) is where the measure's fate is unknowable statically, so the analysis emits the localized seam finding and hands that hop to the runtime check. The missing piece is the reader that consumes the predicate and drives this walk.

---

## 2. Non-additive measure: a stock or a ratio summed across the wrong dimension

**SQL that triggers it.** A daily account rollup sums an end-of-day balance across days:

```sql
-- models/marts/account_totals.sql
select
    account_id,
    sum(end_of_day_balance) as total_balance
from {{ ref('stg_account_balances') }}
group by account_id
```

`end_of_day_balance` is a level measured at a point in time. Adding Monday's balance to Tuesday's produces a number with no meaning. The same shape catches a summed percentage and an averaged ratio.

**Why the audit alone cannot settle it.** Nothing flags this today, and nothing can undeclared. A `sum` of a numeric column is the single most common thing a mart does, and the audit has no way to tell a flow (revenue, additive across time) from a stock (a balance, additive across accounts but not across days). The distinction is semantic and lives nowhere in the SQL. The mixed-currency check is the shape of the answer and is instructive: it fires because a currency tag rides the magnitude through the sum and two currencies cannot add. A stock has the same structure, a facet that survives aggregation and forbids one axis of it, and that facet is the piece that does not exist yet.

**DSL that declares the intent.** This mirrors `Money`/`Currency`. A currency makes a mixed-currency sum ill-typed; a stock makes a cross-time sum ill-typed:

```python
class Balance(DomainType):
    amount: Decimal(18, 2)
    as_of: Timestamp  # the time grain this is a level over; summing across it is ill-typed

class StgAccountBalances(ModelContract):
    dbt_model = "stg_account_balances"
    end_of_day_balance: Balance.columns(amount="end_of_day_balance", as_of="balance_date")
```

**How the declaration closes the gap.** The `Balance` facet marks the amount non-additive over its `as_of` grain, and the check is the mixed-currency check's twin. `_aggregate_tag` already computes whether a `SUM` keeps or clears its tag; a two-currency sum keeps a live unit tag and fires `AGGREGATION_NOT_WELL_TYPED`. A `SUM` whose `GROUP BY` does not include the stock's own time grain keeps a live non-additive tag, the same ill-typed signal. Grouping by `balance_date` (summing across accounts within a day) clears it; summing across days does not. The fix the finding points at is to take the balance at the grain's edge instead, `arg_max(end_of_day_balance, balance_date)`.

**What we do with it.** This is the one shape here that needs more than a reader. It needs a non-additive facet in the type algebra: `FieldKind` gains the notion, `classify` recognizes it, and the aggregate tag rule reads it alongside the existing unit and nominal facets. The propagation and aggregation machinery it rides (`domain_type`, `aggregation_depth`, `_aggregate_tag`) already ship. One new facet, no new lineage.

---

## 3. Referential integrity over a nullable or non-unique key

**SQL that triggers it.** An order mart joins a region dimension:

```sql
-- models/marts/fct_orders_enriched.sql
select
    o.order_id,
    o.amount,
    r.region_name
from {{ ref('stg_orders') }} o
join {{ ref('dim_regions') }} r on r.region_id = o.region_id
```

Three ways this join misbehaves, and the substrate can speak to each. A nullable `o.region_id` drops every order with an unset region. A non-unique `dim_regions.region_id` fans `o.amount` out across the duplicate region rows. An order whose `region_id` matches no region is dropped by the inner join.

**Why the audit alone cannot settle it.** The first two already fire undeclared. `detect_join_on_nullable_key` flags the nullable-key drop, and `detect_join_fanout` flags the non-unique-parent fan-out once `dim_regions`'s key is known, which the dbt `unique` test usually supplies. The edge itself is often free too: `dbt_relationship_edges` harvests it from the project's `relationships` tests. What the audit cannot settle is the third, the orphan drop on a well-formed non-null key. An inner join that discards unmatched rows is ordinary SQL, correct almost everywhere, so flagging it undeclared would be noise.

**DSL that declares the intent.**

```python
class StgOrders(ModelContract):
    dbt_model = "stg_orders"
    region_id: ForeignKey("dim_regions.region_id")
```

The same edge is read from an existing dbt `relationships` test, so a project that has one need not restate it.

**How the declaration closes the gap.** The edge lowers to a `ForeignKeyEdge`, and against it the two hazard facts read as referential-integrity hazards on a stated relationship rather than generic join smells the reader has to adjudicate: `nullability` speaks to the silent drop, `uniqueness` to the fan-out. The orphan drop gains the license section 1's drop side did. A declared foreign key says every child is expected to match a parent, so an inner join that silently discards non-matches is reportable, where undeclared it was indistinguishable from an intended filter.

**What we do with it.** The nullable and fan-out findings fire today; the declaration sharpens their framing and supplies the edge where no dbt test does. The orphan drop needs the same drop-side reader section 1 calls for, keyed on the `ForeignKeyEdge` instead of a conservation predicate.

---

## 4. Grain drift: a declared grain silently coarsened or fanned

**SQL that triggers it.** A mart is meant to hold one row per order, but selects from a per-line model without collapsing:

```sql
-- models/marts/fct_orders.sql   (intended grain: one row per order)
select
    ol.order_id,
    ol.line_amount,
    c.customer_name
from {{ ref('fct_order_lines') }} ol
join {{ ref('dim_customers') }} c on c.customer_id = ol.customer_id
```

`fct_order_lines` is one row per line, and nothing here aggregates, so the output is one row per line while every downstream consumer treats it as one row per order. Any `sum` over it double counts, and any join to it fans out.

**Why the audit alone cannot settle it.** Undeclared, only the symptom fires, and only downstream: a consumer that sums `fct_orders` per order trips `detect_cross_model_fanout` when the keys line up. The root cause, that the model is not at the grain it is meant to be, has nothing to contradict without a stated grain. The audit infers `fct_orders`'s candidate keys but has no expectation to test them against, so the model itself reads as fine and the failure surfaces a model or two later.

**DSL that declares the intent.**

```python
class FctOrders(ModelContract):
    dbt_model = "marts.fct_orders"

    @contract
    def one_row_per_order(self):
        return self.grain(per=self.order_id)
```

**How the declaration closes the gap.** `grain(per=...)` lowers to a `CandidateKeySet` fact, and `uniqueness` already computes the model's inferred candidate keys. What the analysis can honestly report is one grade below a contradiction. A per-line key does not disprove the declared grain (every order may happen to have exactly one line), and the key walk is conservative, so the declared key's absence from the inferred set is often the walk's own silence at an unmodeled shape rather than evidence. The finding is therefore "declared grain not established," raised only on a witness: a strictly finer key (`(order_id, line_number)`) survives to the output with no collapse to the declared grain, with coverage tested through the FD closure (`determines`) so a non-minimal grain does not false-fire, the same hardening `detect_join_fanout` uses. It is raised at the model whose grain is unestablished rather than at the downstream sum that eventually trips over it. The fix is to aggregate to the declared grain, or to correct the declaration to the grain the SQL produces.

**What we do with it.** The emitter does not exist yet, and one substrate piece is missing before it can. Uniqueness reconciles declared and inferred keys by meet, so the declared key unions into the flow value and the stored annotation always contains it: a declaration checks itself and passes. The emitter needs the inferred keys as they were *before* the declaration folded in, which the propagator computes and currently discards; recording that pre-reconcile value beside the flow value is the small recording change `refutation-and-verdicts.md` proposes, and this emitter is its first consumer. Downstream, the declared grain still clears consumers exactly as today, the assume-guarantee posture: trust it forward, question it locally. The emitter is then the uniqueness sibling of the domain-type contradiction check (`DOMAIN_TYPE_CONTRADICTION`, `JOIN_KEY_TYPE_MISMATCH`), one grade weaker: those report a genuine contradiction, this reports a declaration the construction does not establish.

---

## 5. Functional-dependency violation across a union or a join

**SQL that triggers it.** An address dimension unions two sources that disagree on the zip-to-city mapping:

```sql
-- models/marts/dim_addresses.sql
select zip, city from {{ ref('stg_us_addresses') }}
union all
select zip, city from {{ ref('stg_intl_addresses') }}
```

If `zip` is meant to determine `city`, the two branches break it the moment one zip maps to two cities. The same assumption underlies a `GROUP BY zip` that selects `city` un-aggregated, which is correct only if the dependency holds.

**Why the audit alone cannot settle it.** Functional dependencies are already in the substrate, but the audit spends them on other checks rather than self-checking them. The recent grounding work reads a declared or inferred `determines` to prove that a fan-out or a nullable key is safe, and stays quiet. Nothing today reads a dependency and asks whether the structure violates it, so a union that merges conflicting mappings passes unremarked.

**DSL that declares the intent.**

```python
class DimAddresses(ModelContract):
    dbt_model = "marts.dim_addresses"

    @contract
    def zip_determines_city(self):
        return self.zip.determines(self.city)
```

**How the declaration closes the gap.** `determines` lowers to an `FDSet` fact carried on the `functional_dependency` property, and it opens two consumers. It sharpens the existing detectors at once, the same way an inferred dependency does, so a fan-out that `zip -> city` proves harmless is suppressed. And it becomes checkable against structure, at the not-established strength: each arm can honour `zip -> city` on its own while the union of the two does not preserve it (the arms may map a shared zip differently), so the honest finding is that the merge fails to establish the declared dependency, with the union named as the defeater. Proving an actual violation would take value-level reasoning about the arms' contents, which is the runtime loop's territory. The fix is a single source of truth for the mapping before the union, or dropping the claim if the data does not support it.

**What we do with it.** The sharpening is live: a declared `determines` reaches the grounding detectors today. The self-check is the missing emitter, the functional-dependency twin of section 4's grain emitter, resting on the same pre-reconcile record: compare the declared dependency against what the structure re-derives, and report where the derivation was defeated.

---

## 6. Dead predicate: a filter or branch on a value the column never takes

**SQL that triggers it.** A downstream model filters a status against a mistyped literal:

```sql
-- models/marts/fct_shipped_orders.sql
select order_id, status
from {{ ref('stg_orders') }}
where status = 'shipd'   -- the value is 'shipped'; this filter is always empty
```

The predicate is valid SQL and raises nothing; it just returns zero rows forever. The same shape is a `CASE` whose arms miss a member (`'delivered'` falling silently to the `ELSE`), and a join whose key value is out of domain.

**Why the audit alone cannot settle it.** Undeclared, the audit has no idea what values `status` takes, so `status = 'shipd'` reads as an ordinary predicate. The zero-declaration path is #36, which grounds an accepted-value set from an `accepted_values` test or a native `CHECK ... IN`. Absent that test the domain is unknown, and nothing marks `'shipd'` impossible.

**DSL that declares the intent.** A `NominalEnum` names the closed set, and the column binds to it:

```python
class OrderStatus(NominalEnum):
    PLACED = "placed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"

class StgOrders(ModelContract):
    dbt_model = "stg_orders"
    status: OrderStatus
```

**How the declaration closes the gap.** The enum is the accepted-value set. A comparison of `status` to a literal outside its members is a dead predicate: the filter can only ever be empty, which is almost never intended. A `CASE` that does not cover the members leaves the uncovered ones falling to the default, which the analysis can name. The set the check reads comes straight from the enum, and #36 supplies the same set for a project that stated it as an `accepted_values` test instead.

**What we do with it.** The substrate side is NominalEnum propagation (#135), which carries the member set to the point of use, and #36 for the zero-declaration set. The missing piece is the check: read the accepted set at a `Compare` or `InSet` (or a `CASE`) and flag a literal outside it. Small, and it rides the AST the proxy already builds and the set #135 or #36 carries.

---

## 7. Range violation: an arithmetic result that breaks a declared bound

**SQL that triggers it.** A margin model subtracts two non-negative amounts:

```sql
-- models/marts/fct_margin.sql
select
    order_id,
    revenue - refunds as net_revenue   -- negative when refunds exceed revenue
from {{ ref('stg_orders') }}
```

`revenue` and `refunds` are each non-negative, but their difference is not. A downstream model or operation that treats `net_revenue` as non-negative (a `log`, a `sqrt`, a ratio denominator, or a column declared non-negative) breaks on the rows where refunds win.

**Why the audit alone cannot settle it.** Undeclared, the audit carries no value ranges, so it cannot know the operands are non-negative nor that their difference can dip below zero. #36 discovers leaf ranges from `dbt_utils.accepted_range` tests or a native `CHECK ... BETWEEN` and provides the interval lattice (meet is intersection, join is hull). Discovery grounds the leaves; it does not push an interval through the subtraction.

**DSL that declares the intent.** `Field` bounds, the vocabulary that already exists:

```python
class StgOrders(ModelContract):
    dbt_model = "stg_orders"
    revenue: Money = Field(non_negative=True)   # ge=0
    refunds: Money = Field(non_negative=True)
```

**How the declaration closes the gap.** The `non_negative` bounds ground leaf intervals `[0, inf)` on `revenue` and `refunds`, the same interval facts #36 discovers from tests. Propagating them through `revenue - refunds` with the interval lattice gives `[0, inf) - [0, inf) = (-inf, inf)`, so `net_revenue` is not provably non-negative. A downstream requirement of non-negativity then has a decidable conflict: the produced interval admits negatives. The check is interval arithmetic over the AST, seeded by declared or #36-discovered leaves.

**What we do with it.** #36 supplies the interval lattice and leaf-range discovery; `Field` bounds supply the declared leaves. The new piece is a value-interval property that pushes intervals through arithmetic, the analog of the other propagated properties, plus the check that compares a produced interval against a required bound. This is a new analysis rather than a reader, but it is seeded by facts we can already ground.

---

## The shared shape

| Contract | Already fires undeclared | What the declaration adds | Wiring it still needs |
|---|---|---|---|
| Conservation | fan-out over-count (keys free from dbt tests) | the drop side; the origin across an opaque hop | a reader over `ResolvedPredicate` |
| Non-additive measure | nothing | the stock-versus-flow distinction entirely | a non-additive facet in the type algebra |
| Referential integrity | nullable-key drop; fan-out on non-unique parent | the orphan drop; the edge where no dbt test exists | a drop-side reader keyed on the `ForeignKeyEdge` |
| Grain drift | the downstream fan-out symptom only | the grain claim itself, raised at the model | an emitter: declared grain versus inferred keys |
| FD violation | (a declared FD sharpens other checks) | a self-check of the dependency | an emitter: declared FD versus structure |
| Dead predicate | only via a #36 `accepted_values` test | the accepted set from a `NominalEnum` | a check reading the set at a predicate or `CASE` (rides #135, #36) |
| Range violation | only via a #36 `accepted_range` test | declared `Field` bounds | a value-interval property plus a bound check (rides #36's lattice) |

The wiring falls into a few kinds. Two shapes want a reader that walks a declared relation across the lineage (conservation, the referential orphan drop). Two want an emitter that compares a declaration against the derivation the substrate computes before the declaration folds in, and reports where it was defeated (grain, FD); both wait on the pre-reconcile record from `refutation-and-verdicts.md`. One wants a facet in the type algebra (non-additive), one a small check over an accepted set (dead predicate), and one a new value-interval property (range). The common thread is the premise: the declaration supplies the intent that turns an ambiguous structural signal into a decision, and where the trace cannot derive the fact, the declaration stands in for it.
