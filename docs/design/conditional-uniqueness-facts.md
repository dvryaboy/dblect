# Conditional uniqueness facts: leveraging `unique` + `where`

Status: deferred. Captured here so we have somewhere to point when a concrete consumer appears.
Audience: engineers working on the uniqueness layer or the uniqueness-aware detectors.

## What dbt is telling us

A dbt generic test of the form

```yaml
- unique:
    column_name: customer_id
    config:
      where: "country = 'US'"
```

asserts that `customer_id` is unique **among rows of the model satisfying `country = 'US'`**. It says nothing about uniqueness over the full row set. This is a real and common pattern: multi-tenant projects where the natural key is unique per tenant, soft-delete schemas where uniqueness only applies to the live partition, "active" vs "archived" splits, and so on.

The current uniqueness layer in [`facts.py`](../../src/dblect/uniqueness/facts.py) skips these tests entirely. It does so for soundness: a `UniquenessFact` as currently shaped is an unconditional claim, and the uniqueness-aware detectors trust it as such. Promoting a conditional assertion to an unconditional fact would cause silent false negatives downstream.

## What we lose

Skipping is sound but lossy. Projects that lean on partial uniqueness get no help from the uniqueness-aware detector even though the test author has supplied a precise structural fact about the model. In practice this means window-function patterns like

```sql
ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY updated_at)
```

are unverifiable against the model's stated key when that key is conditional, even when the consumer's query carries the same filter.

## Sketch of a future design

A natural shape is a `ConditionalUniquenessFact` variant that carries the predicate alongside the columns:

```python
@dataclass(frozen=True, slots=True)
class ConditionalUniquenessFact:
    model_unique_id: str
    columns: frozenset[str]
    where: str         # the predicate the claim is scoped to
    source: UniquenessSource
    detail: str | None
```

The activation question (when does a conditional fact apply to a given consumer query?) admits several answers, in increasing power and increasing risk:

1. **Exact textual match.** The consumer's containing `SELECT` has a `WHERE` clause whose source text equals the test's `where`. Trivially sound, rarely triggers in real code.
2. **AST-normalized equality.** Parse both predicates with sqlglot, normalize identifier casing and whitespace, compare the trees. Modestly more permissive, still sound. Handles paraphrases like `"country='US'"` versus `"country = 'US'"` and `WHERE country = 'US'` versus `WHERE (country = 'US')`.
3. **Predicate subsumption.** Prove that the consumer's `WHERE` implies the test's `where` (so `country='US' AND state='CA'` activates a fact scoped to `country='US'`). This is full SAT/SMT territory once you allow arithmetic, `IN` lists, or function calls; doing it correctly is a sizeable commitment, and doing it incorrectly produces silent false negatives.

The cheapest sound option that does useful work is (2). It catches the common case where the consumer copies or lightly edits the test's filter, and it never over-claims.

## Why we are deferring

The uniqueness layer's design rests on a hard principle: facts must be rock-solid, because downstream detectors silently rely on them. Introducing a conditional dimension widens the surface where a fact-shaped object can be wrong about the world. We want at least one concrete consumer (a real project that asks for this, ideally with a representative shape we can test against) before committing to an activation rule, so the rule we pick is grounded in observed dbt code rather than guesswork.

The fix already in place (skip `where`-filtered tests entirely) is sound, narrowly scoped, and easy to revisit. The cost of doing nothing now is bounded incompleteness, which fits the rest of the audit's posture: silent when we don't know, loud when we do.

## What to do when we revisit

A reasonable first step is the **capture-first refactor**: replace the current skip with a stored predicate on the fact, without yet wiring activation logic into the detectors. That preserves provenance, lets us see how often partial-uniqueness tests appear in real manifests, and gives us substrate for whichever activation rule we eventually pick. Detector behavior would be unchanged on that step: a fact with a non-empty `where` would still be ignored by consumers, just visibly so. Activation is then a focused follow-up against a concrete example.
