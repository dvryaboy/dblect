# Lineage facts: grounding column annotations from declarations

Status: design
Audience: engineers working on the lineage substrate, on a `Property[K]` that needs column values from manifest declarations or developer assertions, or on the flag system that will eventually feed configuration values into property propagation.

## Motivation

The substrate from [`column-level-lineage.md`](./column-level-lineage.md) gives every `Property[K]` a graph to propagate through, but it doesn't say *where K-values enter the graph*. Each property's `source: Callable[[ColumnRef], K]` rule has to invent its own grounding, and today the demo properties hard-code constants (`UNKNOWN` for nullability, `0` for aggregation depth) because there is no shared way to read `not_null` tests, declared column types, native constraints, or developer column-level refinement declarations off the manifest.

This makes the substrate a barrier to the project's headline capability. The real win is letting a developer declare a refinement, like "this column is `Currency(USD)`" or "this column is `PositiveInt`", on the column where the meaning lives (often a mart-level model, not a raw source), and have the framework do two things with it at once: propagate the refinement downstream as the contract callers can rely on, and verify it against the SQL that produces the column from upstream. Without a facts module, every refinement-type property reimplements manifest plumbing, picks its own precedence rules, and tests its own discovery code. Soundness regressions become easy to introduce and impossible to spot.

A `lineage.facts` module turns this into a substrate-level concern. It mirrors the uniqueness layer's [`facts.py`](../../src/dblect/uniqueness/facts.py) in posture (rock-solid claims, soundness over completeness, opportunistic detector consumption) but at column rather than model granularity, and parameterised on the property's `K` rather than fixed to one axis. The same module is the bridge to the flag system: when a config or var carries a refinement, its fact-shaped contribution feeds the same pipeline.

## What a fact is

A **lineage fact** is a typed claim about one column's value under one property, with provenance. It is *not* a propagated annotation: facts enter the graph from outside (manifest declarations, developer assertions, future config/var sources), the propagator carries them across operators. A column with two facts on the same axis gets the property's combine rule applied; a column with no fact and no upstream falls to the property's documented default; a column with a fact and an upstream is the load-bearing case (see Property integration).

Facts apply at *any* `ColumnRef`, not only at true leaves. The distinction that does matter for the propagator's behaviour is whether the column also has a projection expression in the lineage graph:

- **Anchoring fact.** The column is a source or seed: there is no upstream and no expression. The fact is the only thing the propagator has, and it starts propagation.
- **Asserted fact.** The column is derived (a model output, a CTE intermediate). The fact is a developer or contract claim about what that derivation should produce. The propagator both uses it forward (downstream models inherit the asserted K) and verifies it against the upstream computation.

The contract is the one the uniqueness layer holds today: facts must be rock-solid because downstream detectors silently rely on them. A wrong fact produces a wrong annotation produces a false-positive finding. An absent fact produces a missing annotation produces a silent skip. The audit is louder when it knows and quieter when it does not, never the other way around.

## Position relative to existing substrate

Lineage facts sit one layer below `Property[K]` and one layer above the manifest. The dependency graph:

```
   audit detectors
          ↓
   Property[K] + propagate(graph, prop)
          ↓
   lineage.facts          ←  parallels uniqueness.facts (different K, different key)
          ↓
   Manifest (Node, Column, DbtTestMetadata, ConstraintSpec, …)
```

Three things this module is *not*:

- It is not the lineage graph builder. The builder produces the structural substrate; facts produce the per-property K-values that enter propagation. They run in independent passes and share only `ColumnRef`.
- It is not the uniqueness facts module. Uniqueness facts are model-keyed (`(model_uid, columns)` is the natural identity of a candidate key) and live in their own layer because the uniqueness algebra is the candidate-key lattice, not a column property. Lineage facts are column-keyed and parameterised on `K`. If the column-level reframe of uniqueness (uniqueness as a column-level property over a candidate-key semiring) lands, the two layers converge; until then they are siblings.
- It is not the flag world enumerator from [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md). World enumeration parameterises propagation over flag assignments; this module supplies the per-column values that the assignment-conditioned lookup consumes inside one world.

## Data model

```python
from typing import Generic, Mapping, Protocol, TypeVar
from dataclasses import dataclass
from enum import StrEnum

from dblect.lineage.graph import ColumnRef

K = TypeVar("K")


class FactSource(StrEnum):
    """Where a fact came from. Roughly ranked by trust for tie-breaking."""

    NATIVE_CONSTRAINT      = "native_constraint"      # dbt 1.5+ constraints
    MODEL_CONTRACT         = "model_contract"         # ModelContract declarations
    DBT_GENERIC_TEST       = "dbt_generic_test"       # not_null, accepted_values, …
    DBT_UTILS_TEST         = "dbt_utils_test"         # dbt-utils generic tests
    COLUMN_METADATA        = "column_metadata"        # data_type, nullable in yaml
    DBT_CONFIG             = "dbt_config"             # node.config[...] keys
    DBT_VAR                = "dbt_var"                # vars from dbt_project.yml
    USER_ASSERTED          = "user_asserted"          # explicit override in audit config


@dataclass(frozen=True, slots=True)
class ColumnFact(Generic[K]):
    """One claim about one column under one property.

    ``column`` keys the fact. ``value`` is the K-value the property uses
    when the fact applies. ``source`` records where the claim came from so reviewers
    can audit the chain. ``detail`` is the human-readable why (e.g.,
    ``"not_null test in models/schema.yml:42"``).

    Multiple facts on the same column are intentional, not an error: the
    property combines them via its semiring at source time. Facts are
    deduplicated on identity, not on value.
    """

    column: ColumnRef
    value:  K
    source: FactSource
    detail: str | None = None


FactsByColumn = Mapping[ColumnRef, tuple[ColumnFact[K], ...]]


class FactDiscoverer(Protocol[K]):
    """Discover facts of one axis on one manifest pass.

    A discoverer reads the manifest and yields ``ColumnFact[K]``s for any
    column it can ground. Discoverers are pure: same manifest in, same facts
    out, no caching of mutable state.
    """

    def discover(
        self,
        manifest: "Manifest",
        *,
        name_to_source: Mapping[str, SourceRef],
    ) -> Iterable[ColumnFact[K]]: ...
```

A property that wants facts declares a tuple of discoverers, a combine rule for stacking multiple facts on the same column, and a `consistent` predicate for the case where both a fact and an inferred K exist on a column (see Property integration). The factory `fact_lookup` builds the inner callable:

```python
def fact_lookup(
    facts: FactsByColumn[K],
    *,
    combine: Callable[[K, K], K],
    default: K,
) -> Callable[[ColumnRef], K | None]:
    """Returns the folded fact for a column, or ``None`` when no fact applies.

    ``None`` is distinct from ``default``: it means the propagator should
    fall through to its normal walk. ``default`` is what the propagator
    uses when there's no fact *and* no expression to walk.

    ``combine`` is the property's choice for "two facts on one column."
    Nullability and other lattice-shaped K's use ``semiring.times``
    (every claim holds). Type-like K's where disagreement is a hard error
    use a strict combiner that raises. Accumulating axes use a custom
    fold.
    """
    def lookup(col: ColumnRef) -> K | None:
        bucket = facts.get(col)
        if not bucket:
            return None
        return reduce(combine, (f.value for f in bucket))
    return lookup
```

The combine rule is the property's choice because the meaning of "two facts on one column" is K-specific. Nullability: a `not_null` test and a declared `nullable: true` flag conflict, and either the resolver fails loudly or the property declares which source wins. Type: two `data_type` facts that disagree are a hard error. Refinement-axis facts: multiple are the conjunction.

## Discovery rules

A discoverer per axis. The substrate ships discoverers for the axes that production properties need first; user properties register their own. The shipping set:

| Axis                       | Manifest input                                                | Fact type                |
|----------------------------|---------------------------------------------------------------|--------------------------|
| Nullability                | `not_null` tests, column `nullable` flag, native `NOT NULL` constraint | `ColumnFact[Nullability]` |
| Type                       | column `data_type`                                            | `ColumnFact[SqlType]`     |
| Accepted-values            | `accepted_values` test, native `CHECK ... IN (...)`           | `ColumnFact[frozenset[str]]` |
| Range                      | `dbt_utils.accepted_range`, native `CHECK x BETWEEN ...`      | `ColumnFact[Interval]`    |
| Tags / meta                | column-level `tags` and `meta` keys                           | per-property `ColumnFact[...]` |

Two axes are explicitly forward-looking and stubbed:

- **Config-derived facts.** A `dbt_config` discoverer reads `node.config` keys a property is interested in (e.g., `materialized`, `incremental_strategy`) and produces facts on the model's output columns. The plumbing lands with this module; the per-key fact mappings land as concrete refinements adopt them.
- **Var-derived facts.** A `dbt_var` discoverer reads `vars` from the project config and produces facts where a refinement type's `affects` clause has a single-value mapping. Multi-value mappings remain in the world-enumeration scope of the flag system. The two layers compose: world enumeration picks a flag assignment, the var discoverer produces facts under that assignment, and the property's source rule consumes them.

A discoverer must be pure and total within its axis. "Total" means: every column the discoverer claims authority over either gets a fact or is silently skipped (no `value=unknown` facts pretending to be claims). The default value for a column with no fact comes from `source_rule_from_facts`, not from the discoverer.

## Property integration

A property's `K` is a type, the propagator does type inference over it, and a fact is a type annotation at a `ColumnRef`. The substrate combines them the way a compiler does, not via a dispatch dial.

At each column, the propagator has up to two inputs:

- The **inferred K**, computed by walking the column's projection expression. Absent when the column has no expression (sources, seeds).
- The **declared K**, supplied by `fact_lookup`. Absent when no fact lands on the column.

The output K is determined by which inputs are present, in the order any compiler resolves them:

| Inferred | Declared | Output K          | Behaviour                                                          |
|----------|----------|-------------------|--------------------------------------------------------------------|
| absent   | absent   | `prop.default()`  | No information.                                                    |
| present  | absent   | inferred          | Standard propagation.                                              |
| absent   | present  | declared          | The declaration anchors the column (source, seed, opaque upstream).|
| present  | present  | declared          | Subject to a subtyping check.                                      |

The last row is where the design earns its keep. The property declares a `consistent: Callable[[K, K], bool]` predicate, evaluated as `consistent(declared, inferred)`. It expresses "the inferred K is at least as specific as what the declaration committed to." When it holds, the declared K is the column's annotation downstream because the declaration is the contract callers built against. When it fails, the inferred K is strictly more permissive than the declaration admits, and the audit surfaces it as a finding; downstream still sees the declared K so one upstream regression does not blank analysis of every consumer.

For lattice-shaped K's (most of them), `consistent` is the lattice's precision order. For nullability:

```python
def consistent(declared: Nullability, inferred: Nullability) -> bool:
    """``declared`` holds if ``inferred`` admits no values ``declared`` forbids."""
    # NON_NULL declared: inferred must be NON_NULL, or UNKNOWN if upstream is opaque.
    # NULLABLE or UNKNOWN declared: any inference is consistent (declaration is a weakening).
    if declared is Nullability.NON_NULL:
        return inferred is not Nullability.NULLABLE
    return True
```

The opaque-upstream case (a macro the analyser cannot see through) reaches this rule with `inferred = Nullability.UNKNOWN` and passes vacuously: `UNKNOWN` is the lattice top, so any declaration refines it. No "trust me" mode is needed because the lattice already encodes "I have no information that contradicts the declaration." A property whose K does not have a natural precision order can supply equality, or can decline to opt into facts.

A property exposes a constructor that bundles facts, the consistency rule, and the default:

```python
class Nullability(StrEnum):
    NON_NULL = "non_null"
    NULLABLE = "nullable"
    UNKNOWN  = "unknown"


def nullability_with_facts(
    manifest: Manifest,
    *,
    name_to_source: Mapping[str, SourceRef],
    extra_discoverers: tuple[FactDiscoverer[Nullability], ...] = (),
) -> Property[Nullability]:
    """Build the nullability property with facts grounded from manifest.

    Combines the shipping discoverers (``not_null`` tests, column
    ``nullable`` flag, native NOT NULL constraints) with any caller-supplied
    extras. ``consistent`` is the standard nullability precision order; a
    declared ``not_null`` on a derived column whose upstream is inferred
    ``NULLABLE`` is a type error surfaced through the audit reporter.
    """
    facts = collect_facts(
        manifest,
        discoverers=(*_default_nullability_discoverers, *extra_discoverers),
        name_to_source=name_to_source,
    )
    return Property(
        name="nullability",
        semiring=NullabilitySemiring(),
        facts=fact_lookup(
            facts,
            combine=NullabilitySemiring().times,
            default=Nullability.UNKNOWN,
        ),
        consistent=_nullability_consistent,
        operators={...},
        aggregates={...},
        unknown_value=Nullability.UNKNOWN,
    )
```

This extends the `Property[K]` API in [`column-level-lineage.md`](./column-level-lineage.md) by exactly two callables: `facts` (the lookup) and `consistent` (the subtyping check). Properties that don't opt into facts (`where_provenance`, `aggregation_depth` today) supply a `facts` callable that returns `None` everywhere and never reach the subtyping check.

## Where facts come from

The two roles in "What a fact is" map to different manifest signals.

Anchoring facts come from declarations on the *source* node: `not_null` on a source column, declared `data_type` on a seed, native constraint on a snapshot column. The discoverers in the next section produce them.

Asserted facts come from declarations on a model: a column-level test on a model output, a model contract, a refinement type bound to a `(model_uid, column)` (when the types layer lands). Same discoverers, same `ColumnFact[K]` shape. The propagator does not distinguish at the type level; what changes is whether the column also has an expression in the lineage graph, and that's a property of the graph, not the fact.

This is what makes the design extend naturally to the developer-refinement story: declaring `B.amount: Money(USD)` is the same operation as declaring `raw_orders.amount: int`; only the column it lands on differs, and the property's `consistent` check handles the rest.

## Soundness contract

Same posture as uniqueness facts, restated for the column level:

1. **Discoverer correctness is a hard guarantee.** A discoverer that emits a fact the manifest does not support is a substrate-level bug. PBT covers each shipping discoverer.
2. **Absence is silence, not a default fact.** A column the manifest does not cover is absent from the fact store. `source_rule_from_facts` returns the property default for it. Detectors interpret the default as "we don't know."
3. **Conditional facts are captured but not activated yet.** A `not_null` test with a `where` filter, or a `dbt_utils.accepted_range` scoped to `country = 'US'`, produces a fact-shaped object with the predicate attached, but the standard `source_rule_from_facts` ignores conditional facts. Activation is a follow-up that picks an activation rule consistent with the rule [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md) commits to.
4. **Sources for a fact compose by precedence, not by guess.** When two discoverers produce different facts on the same column for the same axis (a `not_null` test says NON_NULL, a column `nullable: true` flag says NULLABLE), the property's `combine` rule decides. The default is to surface a build-time diagnostic and pick the higher-trust source per `FactSource`'s rank; a property can override.
5. **Facts cross model boundaries only through propagation.** Facts apply to the column on the model that declared them. The propagator carries the K value to downstream models through the lineage graph. There is no facility for "this fact applies to all downstream models" outside the propagator's reach.
6. **Asserted facts are checked, not assumed.** A fact on a derived column is run through the property's `consistent` predicate against what the propagator computes from the upstream. A mismatch is a finding, not a silent acceptance. The "we cannot see through the upstream" case is not an escape hatch; it is the normal output of inference (`prop.default()`, lattice top) and the consistency rule passes vacuously, the same way a compiler accepts a type annotation on an opaque value.

## Failure modes

- **Manifest missing the data a discoverer wants.** The discoverer yields nothing for that column. Property source rule returns default. Diagnostic: the audit report counts how many columns each discoverer found facts for, so reviewers see when a manifest is sparse.
- **Conflicting facts within a single source.** A test and a contract on the same column with incompatible claims is a manifest bug; the audit surfaces it as a `BuildIssue` and the property's `combine` returns its preferred value. The audit never silently picks one and continues.
- **Discoverer raises.** Caught at the discovery layer, surfaced as a `BuildIssue` for the affected model, and the discoverer's facts for that model are dropped. Other discoverers proceed.

## What this does not cover

- **Activation of conditional facts.** See [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md); the same activation question applies here, and the same deferral applies until a concrete consumer asks.
- **World enumeration over flag values.** Belongs to the flag system. This module supplies column values inside a world; the flag layer chooses the world.
- **Cross-package fact inference.** Facts declared in a dbt package and consumed by a downstream package that does not import it. Same scope cut as [`var-inference-spec.md`](./var-inference-spec.md).
- **Runtime facts from the warehouse.** Reading `INFORMATION_SCHEMA.COLUMNS` for an actually-deployed column's nullability, types via the adapter. Useful but distinct from manifest-derived static facts; lands when an adapter-aware fact source is requested.
- **Inference from SQL.** A column projected as `COALESCE(x, 0)` could ground a nullability fact even without a `not_null` test. That work belongs in the property's operator rules (the propagator already does it). Discoverers consult the manifest, not the SQL.

## Sequencing

1. The data model: `ColumnFact[K]`, `FactSource`, `FactsByColumn`, `FactDiscoverer`, `fact_lookup`, `collect_facts`. Two callables added to `Property[K]`: `facts` (the lookup) and `consistent` (the subtyping check). Propagator change: consult `facts` at every column and run `consistent` whenever both an inferred and a declared K are present.
2. Nullability discoverers (`not_null` test, column `nullable`, native `NOT NULL`), nullability promoted to a production property with `consistent` set to the nullability precision order. Closes the source-rule piece of [`#26`](https://github.com/dvryaboy/dblect/issues/26) and lets a `not_null` declared on a derived column catch upstream-changed-to-nullable regressions.
3. Type discoverer (column `data_type`). First property that consumes it is downstream of the semantic-types substrate.
4. Accepted-values and range discoverers. Power the first wave of developer-defined refinements (`PositiveInt`, `Country`, …).
5. Config discoverer with concrete per-key fact mappings as detectors adopt them.
6. Var discoverer wired to single-value flag assignments. Bridge to the flag world enumerator.

Steps 1 and 2 are one PR. The rest are independent and can land in any order driven by the consumer.

## Testing

- **Per-discoverer PBT.** Generate manifests with random combinations of column-level metadata; assert each discoverer's facts are a function of the manifest input it documents, never invent claims, and never drop claims it should produce.
- **Combine-rule PBT.** For each property's chosen `combine`, assert associativity and commutativity hold on the discovered facts so reordering discoverers does not change the source rule.
- **Soundness round-trip on jaffle.** For nullability with manifest-derived facts, assert that every column the audit annotates `NON_NULL` is either a column with a `not_null` test or a column whose projection expression makes it `NON_NULL` (e.g., `COALESCE(x, 0)`). Catches a discoverer that over-claims and a propagator rule that under-checks at the same time.
- **Subtyping check.** A model with a `not_null` declaration on `B.amount` whose upstream infers `NULLABLE` must surface a finding. The contrapositive: a declaration whose upstream infers a consistent K must propagate to downstream models unchanged and produce no finding. A declaration whose upstream infers `UNKNOWN` (opaque macro, dialect gap) must propagate without a finding, by the same `consistent` check, falling out of the lattice rather than a special escape hatch.
- **Conditional-fact capture.** A `not_null` test with a `where` filter must produce a fact with the predicate attached and the standard `fact_lookup` must ignore it. Pins the deferred-activation contract.
- **`consistent` is a partial order.** PBT on each property's `consistent`: reflexivity (`consistent(k, k)`), and that the relation respects the property's semiring (specifically, that `consistent(declared, prop.default())` holds, so opaque upstreams never produce findings).

## References

- The substrate this layers on: [`column-level-lineage.md`](./column-level-lineage.md).
- The model-level facts precedent: [`../../src/dblect/uniqueness/facts.py`](../../src/dblect/uniqueness/facts.py) and the soundness posture in [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- The long-term consumer of var-derived facts: [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md) and the discovery side, [`var-inference-spec.md`](./var-inference-spec.md).
- Issue [`#26`](https://github.com/dvryaboy/dblect/issues/26): promotes the demo nullability + aggregation-depth properties; the source-rule piece is what this module unblocks.
