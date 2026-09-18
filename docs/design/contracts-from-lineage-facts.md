# Contracts an open lineage ticket delivers

*Status: working design notes. Companion to `intent-supplying-contracts.md`. Those contracts are checkable against facts the substrate already carries or the declaration itself states. The three here differ in one respect: their check compares the declaration against a fact the substrate does not yet ground, and each is tracked by an open lineage ticket. This doc pairs each contract with the ticket that delivers it, so the ticket reads as the capability it buys rather than plumbing. Two of the three (seam erasure, unenforced constraint) are delivered whole by their ticket; the third (cardinality) is the ticket's fact plus the shared predicate reader.*

## Where these sit relative to the companion doc

The dividing line is where the fact the check needs comes from. In `intent-supplying-contracts.md` the fact is carried today (uniqueness, nullability, domain types) or supplied by the declaration (a `NominalEnum`'s members, a `Field` bound). Here the fact has to be discovered from the project or the warehouse: a physical type, a relation's row count, whether a native constraint is actually enforced. The discoverer is the open ticket. The contract is what the discovered fact then powers.

---

## 1. Type-seam erasure: a domain type silently dropped at an untyped boundary

**SQL that triggers it.** A typed amount flows through a model that casts it to a bare string:

```sql
-- models/staging/stg_amounts.sql
select
    order_id,
    cast(amount as varchar) as amount   -- the Money meaning is erased here
from {{ ref('raw_orders') }}
```

Upstream `amount` is `Money`; the cast lands it as a plain string. Downstream, `amount` is untyped, so the mixed-currency check and every other type-driven finding go quiet, not because the risk is gone but because the meaning that carried the risk was erased.

**Why the audit alone cannot settle it.** The domain-type property grounds an implicit top at a derived column and lets the typed refinement flow. At a cast to an untyped scalar the refinement clears, and `combine` already detects this and raises `SeamContradictionError`. The detection exists; the finding does not. Nothing emits `refinement-erased-at-seam` yet, so the erasure passes silently.

**DSL that declares the intent.** No new surface: the upstream `Money` binding is what makes the seam a seam.

```python
class RawOrders(ModelContract):
    dbt_model = "raw_orders"
    amount: Money.columns(amount="amount", currency="currency")
```

**How the declaration closes the gap.** With `amount` typed upstream, the cast in `stg_amounts` is a boundary where a typed refinement meets an untyped scalar. The declaration is what puts the type there for the seam to erase.

**Ticket.** #47 is this contract. The seam detection (`combine` / `SeamContradictionError`) already runs; #47 implements the diagnostic (site, operator, both operand columns and types, the axis that cleared, the suppression path), on at the typed layer and off at the zero-declaration layer. No connective ticket is needed beyond it. The physical-type discoverer (#35) is adjacent but not required here: this seam rides the domain-type refinement, not the warehouse `data_type`.

---

## 2. Cardinality bound: a model that must stay 1:1 with its source, or under a size

**SQL that triggers it.** A staging model meant to be one row per source customer joins a plan dimension:

```sql
-- models/staging/stg_customers.sql   (expected 1:1 with app.customers)
select c.*, p.plan_name
from {{ source('app', 'customers') }} c
join {{ ref('dim_plans') }} p on p.plan_id = c.plan_id
```

If `dim_plans.plan_id` is not unique, the join inflates the row count, and a model meant to preserve its source's cardinality silently gains rows. The bounded-size variant is a dimension a downstream step assumes stays under some size.

**Why the audit alone cannot settle it.** The fan-out itself may fire (`detect_join_fanout`, once the plan key is known), but the contract "this model is 1:1 with its source" has no cardinality fact to check against. The audit does not carry relation row counts, so the 1:1 claim is uncheckable, and a bounded-size claim has nothing to compare a count to.

**DSL that declares the intent.** A count predicate over the two relations:

```python
class StgCustomers(ModelContract):
    dbt_model = "stg_customers"

    @contract
    def one_row_per_source_customer(self):
        return self.customer_id.count() == models.customers.customer_id.count()
```

A size bound is the same shape with a literal: `self.customer_id.count() <= 5_000_000`.

**How the declaration closes the gap.** #38 grounds a row-count interval per relation from a `dbt_utils.expression_is_true` count assertion. The count predicate is a `ResolvedPredicate` like conservation's, and evaluating it against the row-count facts decides the claim: an inflated model fails the 1:1 equality, a too-large one fails the bound.

**Ticket.** #38 delivers the row-count fact. The check that evaluates a count predicate against it is the same `ResolvedPredicate` reader conservation needs, so cardinality rides that shared connective ticket rather than one of its own. Existing fact plus the shared predicate reader.

---

## 3. Unenforced constraint: a finding that rests on a bet nothing backs

**Setup that triggers it.** A model declares a native `unique` (or `not null`) constraint that the warehouse does not enforce on write, and no dbt test re-checks it:

```yaml
# models/schema.yml
models:
  - name: dim_products
    columns:
      - name: product_id
        constraints:
          - type: unique   # advisory on this warehouse: enforced_on_write is false
```

The substrate reads the advisory constraint as a uniqueness fact and uses it to clear a downstream Duplicate finding. If the constraint is not actually enforced and nothing tests it, that clearance rests on an unbacked bet, and a real duplicate ships unflagged.

**Why the audit alone cannot settle it.** The constraint grounds a fact like any other. What the audit does not compute is whether the fact is load-bearing (whether dropping it would change what the audit reports) and whether anything actually guarantees it. Without that, an unenforced constraint and a tested one look identical.

**DSL that declares the intent.** No new surface: the constraint is already in the dbt schema. The contract is the trace that decides whether it is load-bearing.

**How the declaration closes the gap.** A helper traces an annotation to its grounding facts and asks whether dropping it to top would change a finding. Where a load-bearing annotation rests on an `enforced_on_write=False` constraint that no running test covers at the same scope, that is the finding. The important case is the suppressed Duplicate detector silenced by an assumed-unique advisory key.

**Ticket.** #48 is this contract, whole: the finding plus the annotation-trace helper it needs to compute load-bearing-ness. No connective ticket beyond it.

---

## The map

| Contract | Fact ticket | Delivered by | New connective work |
|---|---|---|---|
| Type-seam erasure | seam detection already runs | #47 (the diagnostic) | none |
| Cardinality bound | #38 (row-count interval) | #38 plus the predicate reader | shared with conservation |
| Unenforced constraint | the constraint is in the schema | #48 (finding plus trace helper) | none |

Two of the three are delivered by their ticket outright; the third leans on the one connective piece the companion doc also needs, the `ResolvedPredicate` reader. So the discovered-fact contracts add almost no new surface of their own. They are what the open lineage tickets were already going to buy, named as the capability rather than the plumbing.
