# Window functions as per-property boundaries

Status: design notes. Builds on [`lineage-facts.md`](./lineage-facts.md) (the substrate, the `AggregateRule` shape, the per-property erase framing) and cross-references [`semantic-types-layer.md`](./semantic-types-layer.md) (the combinability surface). Cross-references resolve once those land.
Audience: engineers on property propagation, on the uniqueness migration, or on the ordering-determinism detector.

## The reframe

The early scope decision in [`design-concepts-digest.md`](./design-concepts-digest.md) ("Window functions sit out of v1 propagation") treats a window region as an opaque boundary that erases every refinement and re-anchors at the output. That was the right conservative first cut: refinement through windows touches cardinality, ordering, and scope at once, and the prior art on the ordering piece is thin. This doc refines that cut rather than overturning it. The observation is that an erase boundary should be **per-property, not per-region**, and that a window erases far less than the region treatment assumes.

The reason is one structural fact: a window function is row-preserving. Most properties propagate through it soundly, and the only refinement that genuinely erases at a window is ordering-determinism, which is a separate property with its own detector. Separating the row-preserving structural part (cheap and sound) from the ordering-sensitive part (the genuinely hard piece) recovers the highest-value case, the uniqueness that a `ROW_NUMBER` dedup establishes, with no new predicate machinery and without breaking the single-pass walk.

## A window is row-preserving

`f() OVER (PARTITION BY p ORDER BY o [frame])` adds a column. It never merges or drops rows, so the output relation is in bijection with the input. Among SQL constructs, only `GROUP BY`, `DISTINCT`, set operations, `WHERE` / `QUALIFY`, `LIMIT`, and bare aggregation change the row count. A window function is defined to preserve it.

This holds for framed windows too, which is the case most likely to look like a collapse. Take `SUM(amount) OVER (PARTITION BY p ORDER BY o ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)` on a seven-row partition `r1..r7` ordered by `o`:

```
r1 -> sum(r1)            r5 -> sum(r1..r5)
r2 -> sum(r1,r2)         r6 -> sum(r2..r6)
r3 -> sum(r1,r2,r3)      r7 -> sum(r3..r7)
r4 -> sum(r1..r4)
```

Seven rows in, seven rows out. The frame decides which rows feed each output value, not how many output rows exist. Every property that depends only on the one-to-one row correspondence is therefore unaffected by the frame.

## Per-property treatment

Each property gets the treatment its semantics warrant, the same way each property already chooses its operator transfers in the substrate.

### Cardinality

A window is one-to-one, so cardinality is preserved exactly. The fan-out analysis reads through a window unchanged.

### Uniqueness

Three sound rules cover the cases that matter, and none needs predicate reasoning.

- **Existing keys pass through.** Rows are neither merged nor dropped, so every candidate key of the input relation is a candidate key of the output.
- **`ROW_NUMBER` introduces a key.** `ROW_NUMBER() OVER (PARTITION BY p ORDER BY o)` assigns distinct values `1..n` within each partition, so `{p} ∪ {row_number_col}` is a candidate key on the output. This is sound even when `o` has ties: the engine still assigns distinct numbers within the partition, so the pair is unique regardless of how ties break. The nondeterminism is about *which* row receives a given number, which is the ordering-determinism property's concern, not whether the numbers are distinct. The two decouple cleanly. The rule is specific to `ROW_NUMBER` and any function that guarantees per-partition-distinct output. `RANK` and `DENSE_RANK` produce ties on purpose, so `{p, rank}` is not a key and the rule does not fire for them.
- **A constant column drops out of a key.** A filter `WHERE rn = 1` (or the equivalent `QUALIFY`) constrains `rn` to a single value. A column fixed to a constant is functionally determined by everything, so it leaves every candidate key: `{p, rn}` with `rn` constant yields `{p}`. This is the standard behaviour of a functional dependency under selection, and it is a small enhancement to the uniqueness filter transfer (today "preserve", here "preserve, and eliminate an equality-to-literal column from candidate keys").

### Nullability

Window functions have known per-function nullability, registered the same way scalar and aggregate rules are. `ROW_NUMBER`, `RANK`, and `DENSE_RANK` are non-null. `LAG` and `LEAD` can introduce nulls at partition edges (or return their supplied default). A windowed `SUM` or `AVG` follows the nullability of its aggregate over the frame. None of these is a reason to erase; each is a transfer.

### User-domain axes

The value-domain axes raise the combinability question the substrate already answers for `GROUP BY` aggregation, and a window inherits most of that answer.

- **Coherence survives the frame.** The coherence guard for a `GROUP BY` reads the functional dependency `group_keys -> within` (each group is constant on the `within` columns). The window analog reads `p -> within` (each partition is constant on them). Every frame is a subset of its partition, and a subset of a coherent set is coherent, so a framed `SUM(amount: Money)` mixes no currencies whenever the partition is currency-coherent, under the same guard. The frame cannot break coherence, because coherence is a partition-level property inherited by every subframe.
- **Summability carries over.** A sliding sum of a non-summable measure (a ratio, a percentage) is as wrong as a grouped sum of one, so the `summable` core applies unchanged.

What a frame does add is a meaning that may differ from the underlying measure: a cumulative `SUM` of a daily flow is a stock (revenue-to-date), not the flow. The framework cannot infer that distinction, which is the same situation as `revenue * 0.9` ([`design-concepts-digest.md`](./design-concepts-digest.md), "Literals are opaque to refinement propagation"). The conservative treatment preserves the axes it can reason about (currency) and leaves the flow-versus-stock distinction to a declaration if the user cares. This is the general aggregate-meaning question, not anything specific to frames.

## The dedup pattern, end to end

The canonical deduplication idiom is the worked example that ties the rules together:

```sql
SELECT *
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY updated_at) AS rn
  FROM source
)
WHERE rn = 1
```

Two structural steps, both predicate-free:

1. `ROW_NUMBER() OVER (PARTITION BY customer_id ...)` makes `{customer_id, rn}` a candidate key.
2. `WHERE rn = 1` fixes `rn` to a constant, eliminating it from the key, so `{customer_id}` is a candidate key on the result.

The audit now proves the model is unique on `customer_id`, which is the structural fact the modeler intended. Whether the `ORDER BY updated_at` tiebreak is deterministic is reported separately by the ordering-determinism detector. The key holds either way; the determinism finding is about which row survives, not about uniqueness.

### Relationship to conditional uniqueness facts

[`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md) reaches for the dedup pattern through a different door, a `unique` test scoped by a `where` clause, and defers activation because matching a consumer's filter to a test's predicate is full predicate-implication territory. The window treatment here removes the dependency: the dedup pattern is handled by structural propagation with no predicate logic at all. Conditional facts remain useful for the genuinely declarative case, a `unique: customer_id where country = 'US'` whose predicate an arbitrary consumer must discharge, and that activation stays deferred for the reasons that doc gives. The two mechanisms now cover different ground rather than competing for the same example.

## What stays opaque

The cut narrows; it does not vanish.

- **Ordering-determinism is the one refinement a window erases.** Whether a frame is well-defined depends on the `ORDER BY` being a total order. Ties (and `RANGE` frames, which pull peers into the frame) make the per-row value order-sensitive and, under a nondeterministic tiebreak, nondeterministic. That is the property the ordering-hazard detector in the static analyser already owns. This doc hands it that one concern and keeps everything else.
- **Recursive CTEs stay opaque.** A recursive CTE needs a fixpoint that the single-pass walk does not run, so it remains an erase boundary that re-anchors on output. A window needs no fixpoint: it is one node transfer like any other, so de-opaquing it keeps the walk single-pass. This is the line between the two constructs the substrate previously grouped together.

## Implementation sketch

The new surface is small because it reuses the substrate.

- **A window transfer dispatches on the inner function.** sqlglot models `ROW_NUMBER() OVER (...)` as a `Window` node wrapping the function, so the window transfer unwraps the `Window`, reads the partition and order keys from the frame, and dispatches on the inner `exp.AggFunc` or ranking function. Value-domain windows reuse the property's existing `AggregateRule` registry (the `core` plus the optional `CoherenceGuard`), with the guard reading `p -> within` instead of `group_keys -> within`. Ranking functions install the key-introduction rule.
- **The uniqueness filter transfer gains constant-elimination.** Recognising an equality-to-literal predicate and dropping the fixed column from candidate keys is local to the uniqueness property's `Where` transfer.
- **The walk is unchanged in shape.** A window is a node with a derivation, so the propagator already visits it; the change is that the visit computes a transfer rather than re-anchoring to top.

## Soundness obligations

- **Row-preservation is the load-bearing fact** behind cardinality preservation, existing-key preservation, and the bijection that makes the `ROW_NUMBER` key sound. It is a property of SQL window semantics, true in every dialect sqlglot parses.
- **Key-introduction is restricted to functions that guarantee per-partition-distinct output.** `ROW_NUMBER` qualifies; `RANK` and `DENSE_RANK` do not. The PBT generates partitions with duplicate order keys and asserts `{p, row_number}` is a key while `{p, rank}` is not.
- **Coherence inheritance** (a subframe of a coherent partition is coherent) is the obligation that lets the existing coherence guard carry to framed windows without change.
- **Determinism is out of scope here by construction.** The key and dedup rules are proven independent of tiebreak determinism, so the ordering-determinism detector and the uniqueness propagation never need to agree on a tiebreak.

## Sequencing

This rides with the consumers that need it, not with the substrate that enables it.

- The structural rules (cardinality, existing keys, `ROW_NUMBER` key-introduction, constant-elimination) land with the uniqueness migration, since that is where the relation-algebra walk and the candidate-key encoding come online.
- The value-domain rules (coherence through windows, summability) land with the value-domain aggregate work, since they reuse the `AggregateRule` machinery.
- A conservative intermediate is available at every step: a property that has not yet grown a window transfer re-anchors at the window output exactly as today, so adopting this is incremental and never regresses a property that opts out.

## What this does not cover

- **Recursive-CTE propagation.** It needs a fixpoint and stays an opaque boundary.
- **The full value semantics of framed windows for user-domain meaning-shifts.** Currency-coherence and summability carry over; the flow-versus-stock distinction a cumulative window can introduce is a declaration question, the same as any aggregate or scalar meaning-transform.
- **Ordering-determinism itself.** That property lives in the ordering-hazard detector; this doc only establishes that uniqueness propagation does not depend on it.
- **Predicate-implication activation of conditional facts.** The dedup pattern no longer needs it; the declarative `where`-scoped case still defers to [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).

## Open questions

- How far to push key-introduction beyond `ROW_NUMBER`. A `COUNT(*) OVER (PARTITION BY p)` equal to the partition size, or a `DENSE_RANK` combined with a known-unique order, can imply structure, but the marginal value over `ROW_NUMBER` is unclear and the soundness side-conditions grow.
- Whether constant-elimination should generalise from equality-to-literal to other functionally-determining predicates (an `IN` list of one element, a join to a single-row dimension). The equality case covers the dedup idiom; the rest is incremental.
- Whether the value-meaning shift a cumulative window introduces deserves a dedicated annotation (a `dblect: cumulative` marker) or stays under the general scalar-annotation surface.

## References

- [`lineage-facts.md`](./lineage-facts.md): the substrate, the `AggregateRule` and `CoherenceGuard` shapes the window transfer reuses, and the per-property erase framing this doc applies to windows.
- [`column-level-lineage.md`](./column-level-lineage.md): the propagation engine and the candidate-key encoding the structural rules extend.
- [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md): the declarative partial-uniqueness case the dedup pattern complements.
- [`design-concepts-digest.md`](./design-concepts-digest.md): the original "windows sit out of v1 propagation" decision this doc refines, and the opaque-literal and ordering-hazard treatments it builds on.
- [`semantic-types-layer.md`](./semantic-types-layer.md): the combinability surface (`within`, `summable`) the value-domain window rules inherit.
- Functional-dependency propagation under selection and projection (Abiteboul, Hull, Vianu) for the constant-elimination and key-preservation rules. SQL window-function semantics (the SQL standard's `OVER` clause; the framing in HoTTSQL and Cosette for the row-preserving core) for the cardinality and bijection facts.
