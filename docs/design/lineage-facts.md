# Lineage facts: grounding column annotations from declarations

Status: design
Audience: engineers working on the lineage substrate, on a `Property[K]` that needs column values from manifest declarations or developer assertions, or on the flag system that will eventually feed configuration values into property propagation.

## Motivation

The substrate from [`column-level-lineage.md`](./column-level-lineage.md) gives every `Property[K]` a graph to propagate through. It does not say where K-values enter the graph. Each property's `source: Callable[[ColumnRef], K]` rule has to invent its own grounding, and today the demo properties hard-code constants (`UNKNOWN` for nullability, `0` for aggregation depth) because there is no shared way to read `not_null` tests, declared column types, native constraints, or developer column-level refinement declarations off the manifest.

The win this unlocks is letting a developer declare a refinement, like `Currency(USD)` or `PositiveInt`, on the column where the meaning lives (often a mart-level model, not a raw source). The framework then propagates the refinement downstream as the contract callers can rely on, and checks it against the SQL that produces the column from upstream. Without a facts module, every refinement-type property reimplements manifest plumbing, picks its own precedence rules, and tests its own discovery code.

A `lineage.facts` module turns this into a substrate concern. It mirrors the uniqueness layer's [`facts.py`](../../src/dblect/uniqueness/facts.py) in posture (rock-solid claims, soundness over completeness, opportunistic detector consumption) at column granularity rather than model granularity, and parameterised on the property's `K` rather than fixed to one axis. The same module is the bridge to the flag system: when a config or var carries a refinement, its fact-shaped contribution feeds the same pipeline.

## What a fact is

A **lineage fact** is a typed claim about one column's value under one property, with provenance. Facts apply at *any* `ColumnRef`, source or derived. The propagator's behaviour at a column depends on whether the column also has a projection expression in the lineage graph:

- **Anchoring.** No expression (source, seed). The fact is the only input the propagator has.
- **Asserted.** Has an expression (model output, CTE intermediate). The fact is a developer or contract claim about what that derivation produces. The propagator uses it forward and checks it against the upstream.

The contract follows the uniqueness layer: facts must be rock-solid because downstream detectors silently rely on them. A wrong fact produces a wrong annotation produces a false-positive finding. An absent fact produces a missing annotation produces a silent skip. The audit is louder when it knows and quieter when it does not.

## Position relative to existing substrate

```
   audit detectors
          ↓
   Property[K] + propagate(graph, prop)
          ↓
   lineage.facts          ←  sibling of uniqueness.facts
          ↓
   Manifest (Node, Column, DbtTestMetadata, ConstraintSpec, …)
```

Lineage facts are column-keyed and parameterised on `K`; uniqueness facts are model-keyed because a candidate key is naturally identified by `(model_uid, columns)`. The two layers run independently. If a future column-level reframe of uniqueness lands, they converge.

## Data model

```python
from typing import Generic, Mapping, Protocol, TypeVar
from dataclasses import dataclass
from enum import StrEnum

from dblect.lineage.graph import ColumnRef

K = TypeVar("K")


class FactSource(StrEnum):
    """Where a fact came from. Ranked by trust for tie-breaking."""

    NATIVE_CONSTRAINT      = "native_constraint"      # dbt 1.5+ constraints
    MODEL_CONTRACT         = "model_contract"         # ModelContract declarations
    DBT_GENERIC_TEST       = "dbt_generic_test"       # not_null, accepted_values, …
    DBT_UTILS_TEST         = "dbt_utils_test"         # dbt-utils generic tests
    COLUMN_METADATA        = "column_metadata"        # data_type, nullable in yaml
    DBT_CONFIG             = "dbt_config"             # node.config[...] keys
    DBT_VAR                = "dbt_var"                # vars from dbt_project.yml
    USER_ASSERTED          = "user_asserted"          # explicit declaration in audit config


@dataclass(frozen=True, slots=True)
class ColumnFact(Generic[K]):
    """One claim about one column under one property."""

    column: ColumnRef
    value:  K
    source: FactSource
    detail: str | None = None


FactsByColumn = Mapping[ColumnRef, tuple[ColumnFact[K], ...]]


class FactDiscoverer(Protocol[K]):
    """Reads the manifest, yields ``ColumnFact[K]``s for any column it can ground.

    Pure: same manifest in, same facts out, no mutable state.
    """

    def discover(
        self,
        manifest: "Manifest",
        *,
        name_to_source: Mapping[str, SourceRef],
    ) -> Iterable[ColumnFact[K]]: ...
```

`fact_lookup` folds multiple facts on a column via a property-supplied combine rule and returns `None` when no fact applies:

```python
def fact_lookup(
    facts: FactsByColumn[K],
    *,
    combine: Callable[[K, K], K],
) -> Callable[[ColumnRef], K | None]:
    """``None`` means the propagator should fall through to its walk.

    ``combine`` is the property's choice for "two facts on one column."
    Lattice K's use ``semiring.times`` (every claim holds). Type-like K's
    where disagreement is a hard error use a strict combiner that raises.
    Accumulating axes use a custom fold.
    """
    def lookup(col: ColumnRef) -> K | None:
        bucket = facts.get(col)
        if not bucket:
            return None
        return reduce(combine, (f.value for f in bucket))
    return lookup
```

## Discovery rules

A discoverer per axis. The substrate ships discoverers for the axes production properties need first; user properties register their own.

| Axis                       | Manifest input                                                | Fact type                |
|----------------------------|---------------------------------------------------------------|--------------------------|
| Nullability                | `not_null` tests, column `nullable` flag, native `NOT NULL` constraint | `ColumnFact[Nullability]` |
| Type                       | column `data_type`                                            | `ColumnFact[SqlType]`     |
| Accepted-values            | `accepted_values` test, native `CHECK ... IN (...)`           | `ColumnFact[frozenset[str]]` |
| Range                      | `dbt_utils.accepted_range`, native `CHECK x BETWEEN ...`      | `ColumnFact[Interval]`    |
| Tags / meta                | column-level `tags` and `meta` keys                           | per-property `ColumnFact[...]` |

Anchoring facts come from declarations on a source node; asserted facts come from declarations on a model (column-level tests, model contracts, refinement-type bindings when the types layer lands). The discoverer does not distinguish: a `ColumnFact[K]` keyed on a model column lands on a model column.

Two axes are forward-looking:

- **Config-derived facts.** A `dbt_config` discoverer reads `node.config` keys a property is interested in (`materialized`, `incremental_strategy`, …) and produces facts on the model's output columns. Plumbing lands with this module; per-key fact mappings land as concrete refinements adopt them.
- **Var-derived facts.** A `dbt_var` discoverer produces facts where a refinement type's `affects` clause has a single-value mapping. Multi-value mappings remain in the world-enumeration scope of the flag system. World enumeration picks an assignment, the var discoverer produces facts under it, the lookup consumes them.

A discoverer is pure and total within its axis. Total means: every column the discoverer claims authority over either gets a fact or is silently skipped. No `value=unknown` facts pretending to be claims.

## Property integration

A property's `K` is a type, the propagator does inference over it, and a fact is a type annotation at a `ColumnRef`. At each column the propagator has up to two inputs:

- The **inferred K**, from walking the column's expression. Absent for sources and seeds.
- The **declared K**, from `fact_lookup`. Absent when no fact applies.

| Inferred | Declared | Output K          | Behaviour                                                          |
|----------|----------|-------------------|--------------------------------------------------------------------|
| absent   | absent   | `prop.default()`  | No information.                                                    |
| present  | absent   | inferred          | Standard propagation.                                              |
| absent   | present  | declared          | Declaration anchors the column.                                    |
| present  | present  | declared          | Subject to `consistent(declared, inferred)`.                       |

The property declares `consistent: Callable[[K, K], bool]`. It expresses "the inferred K is at least as specific as what the declaration committed to." When it holds, the declared K is the column's annotation downstream (the declaration is the contract callers built against). When it fails, the audit surfaces a finding; downstream still sees the declared K so one upstream regression does not blank analysis of every consumer.

For lattice-shaped K's, `consistent` is the lattice's precision order. For nullability:

```python
def consistent(declared: Nullability, inferred: Nullability) -> bool:
    """``declared`` holds if ``inferred`` admits no values ``declared`` forbids."""
    if declared is Nullability.NON_NULL:
        return inferred is not Nullability.NULLABLE
    return True
```

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
    facts = collect_facts(
        manifest,
        discoverers=(*_default_nullability_discoverers, *extra_discoverers),
        name_to_source=name_to_source,
    )
    return Property(
        name="nullability",
        semiring=NullabilitySemiring(),
        facts=fact_lookup(facts, combine=NullabilitySemiring().times),
        consistent=_nullability_consistent,
        operators={...},
        aggregates={...},
        unknown_value=Nullability.UNKNOWN,
    )
```

`Property[K]` gains two callables: `facts` (the lookup) and `consistent` (the subtyping check). Properties that don't opt into facts (`where_provenance`, `aggregation_depth`) supply a `facts` callable that returns `None` everywhere.

## Soundness contract

1. **Discoverer correctness is a hard guarantee.** A discoverer that emits a fact the manifest does not support is a substrate bug. PBT covers each shipping discoverer.
2. **Absence is silence.** A column the manifest does not cover is absent from the fact store. The propagator returns the property default. Detectors interpret it as "we don't know."
3. **Conditional facts are captured but not activated.** A `not_null` with a `where` filter produces a fact with the predicate attached, but `fact_lookup` ignores it. Activation follows the rule [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md) eventually commits to.
4. **Two discoverers, one column.** When two discoverers produce different facts for the same axis on the same column, the property's `combine` rule decides. The default is to surface a build-time diagnostic and pick the higher-trust source per `FactSource`'s rank.
5. **Facts cross model boundaries only through propagation.** Facts apply to the column on the model that declared them. The propagator carries the K value downstream through the lineage graph.
6. **Asserted facts are checked.** A fact on a derived column runs through `consistent` against the inferred K. A mismatch is a finding. When the upstream is unanalysable, the inferred K is the property default, the lattice top, and `consistent` passes vacuously.

## Failure modes

- **Manifest sparse on a discoverer's axis.** Discoverer yields nothing for that column. Propagator returns the property default. The audit report counts how many columns each discoverer grounded so reviewers see when a manifest is sparse.
- **Conflicting facts within a single source.** A test and a contract on the same column with incompatible claims is a manifest bug; the audit surfaces it as a `BuildIssue` and `combine` returns its preferred value.
- **Discoverer raises.** Caught at the discovery layer, surfaced as a `BuildIssue` for the affected model, its facts for that model are dropped. Other discoverers proceed.

## What this does not cover

- **Activation of conditional facts.** See [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- **World enumeration over flag values.** Belongs to the flag system. This module supplies column values inside a world; the flag layer chooses the world.
- **Cross-package fact inference.** Facts declared in a dbt package and consumed by a downstream package that does not import it. Same scope cut as [`var-inference-spec.md`](./var-inference-spec.md).
- **Runtime facts from the warehouse.** `INFORMATION_SCHEMA.COLUMNS` or adapter-side metadata. Lands when an adapter-aware fact source is requested.
- **Inference from SQL.** A column projected as `COALESCE(x, 0)` could ground a nullability fact even without a `not_null` test. That work lives in the property's operator rules.

## Sequencing

1. The data model: `ColumnFact[K]`, `FactSource`, `FactsByColumn`, `FactDiscoverer`, `fact_lookup`, `collect_facts`. Two callables added to `Property[K]`: `facts` and `consistent`. Propagator change: consult `facts` at every column and run `consistent` whenever both an inferred and a declared K are present.
2. Nullability discoverers (`not_null` test, column `nullable`, native `NOT NULL`), nullability promoted to a production property with `consistent` set to the nullability precision order. Closes the source-rule piece of [`#26`](https://github.com/dvryaboy/dblect/issues/26) and lets a `not_null` declared on a derived column catch upstream-changed-to-nullable regressions.
3. Type discoverer (column `data_type`). First consumer is the semantic-types substrate.
4. Accepted-values and range discoverers. Power the first wave of developer-defined refinements (`PositiveInt`, `Country`, …).
5. Config discoverer with concrete per-key fact mappings as detectors adopt them.
6. Var discoverer wired to single-value flag assignments. Bridge to the flag world enumerator.

Steps 1 and 2 are one PR. The rest are independent and land driven by the consumer.

## Testing

- **Per-discoverer PBT.** Generate manifests with random combinations of column-level metadata; assert each discoverer's facts are a function of its documented manifest input, never invent claims, never drop ones it should produce.
- **Combine-rule PBT.** Associativity and commutativity on each property's chosen `combine`, so reordering discoverers does not change the lookup.
- **Soundness round-trip on jaffle.** For nullability with manifest-derived facts, assert that every column the audit annotates `NON_NULL` either has a `not_null` test or has a projection expression that makes it `NON_NULL` (`COALESCE(x, 0)`). Catches over-claiming discoverers and under-checking propagator rules at the same time.
- **`consistent` PBT.** Reflexivity (`consistent(k, k)`), and `consistent(declared, prop.default())` for every K (so opaque upstreams never produce findings).
- **Asserted-fact end-to-end.** A `not_null` declaration on `B.amount` with `NULLABLE` upstream surfaces a finding; the same declaration with `NON_NULL` or `UNKNOWN` upstream propagates to downstream models without a finding.
- **Conditional-fact capture.** A `not_null` test with a `where` filter produces a fact with the predicate attached; `fact_lookup` ignores it.

## References

- The substrate this layers on: [`column-level-lineage.md`](./column-level-lineage.md).
- The model-level facts precedent: [`../../src/dblect/uniqueness/facts.py`](../../src/dblect/uniqueness/facts.py) and the soundness posture in [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- The long-term consumer of var-derived facts: [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md), and the discovery side, [`var-inference-spec.md`](./var-inference-spec.md).
- Issue [`#26`](https://github.com/dvryaboy/dblect/issues/26): promotes the demo nullability + aggregation-depth properties; the source-rule piece is what this module unblocks.
