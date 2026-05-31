# Lineage facts: grounding leaf annotations from the manifest

Status: design
Audience: engineers working on the lineage substrate, on a `Property[K]` that needs leaf values from manifest metadata, or on the flag system that will eventually feed configuration values into property propagation.

## Motivation

The substrate from [`column-level-lineage.md`](./column-level-lineage.md) gives every `Property[K]` a graph to propagate through, but it doesn't say *where leaf values come from*. Each property's `source: Callable[[ColumnRef], K]` rule has to invent its own leaf grounding: today the demo properties hard-code constants (`UNKNOWN` for nullability, `0` for aggregation depth) because there is no shared way to read `not_null` tests, declared column types, native constraints, or model contracts off the manifest.

This makes the substrate a barrier to the project's headline capability. The real win is letting a developer declare a refinement, like "this column is always positive" or "this column is `Currency(USD)`", and have the framework check that the SQL upholds it. A refinement is a `Property[K]` whose leaves are facts about what the developer has already told us about their sources. Without a facts module, every such property reimplements manifest plumbing, picks its own precedence rules, and tests its own discovery code. Soundness regressions become easy to introduce and impossible to spot.

A `lineage.facts` module turns leaf grounding into a substrate-level concern. It mirrors the uniqueness layer's [`facts.py`](../../src/dblect/uniqueness/facts.py) in posture (rock-solid claims, soundness over completeness, opportunistic detector consumption) but at column rather than model granularity, and parameterised on the property's `K` rather than fixed to one axis. The same module is the bridge to the flag system: when a config or var carries a refinement, its fact-shaped contribution feeds the same source-rule pipeline.

## What a fact is

A **lineage fact** is a claim about one column's value under one property, with provenance. It is *not* a propagated annotation: facts ground leaves, the propagator stitches the rest. A column with two facts on the same axis gets the property semiring's combination of them, not a winner; a column with no fact gets the property's documented default.

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

- It is not the lineage graph builder. The builder produces the structural substrate; facts produce the per-property leaf values. They run in independent passes and share only `ColumnRef`.
- It is not the uniqueness facts module. Uniqueness facts are model-keyed (`(model_uid, columns)` is the natural identity of a candidate key) and live in their own layer because the uniqueness algebra is the candidate-key lattice, not a column property. Lineage facts are column-keyed and parameterised on `K`. If the column-level reframe of uniqueness (uniqueness as a column-level property over a candidate-key semiring) lands, the two layers converge; until then they are siblings.
- It is not the flag world enumerator from [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md). World enumeration parameterises propagation over flag assignments; this module supplies the per-leaf values that the assignment-conditioned source rule consumes inside one world.

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

    ``column`` keys the fact. ``value`` is the K-value the property uses to
    seed the leaf. ``source`` records where the claim came from so reviewers
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

A property that wants facts declares a tuple of discoverers and a combine rule. The factory `source_rule_from_facts` builds the property's `source` callable:

```python
def source_rule_from_facts(
    facts: FactsByColumn[K],
    *,
    combine: Callable[[K, K], K],
    default: K,
) -> Callable[[ColumnRef], K]:
    """The standard pattern: fold multiple facts on a column via ``combine``,
    return ``default`` for columns with none.

    ``combine`` is typically ``semiring.times`` (the column has all claimed
    properties at once) or ``semiring.plus`` (any-of), depending on what
    "two facts on one column" means for the property's K.
    """
    def rule(col: ColumnRef) -> K:
        bucket = facts.get(col)
        if not bucket:
            return default
        return reduce(combine, (f.value for f in bucket))
    return rule
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

## Source-rule integration

A property exposes a constructor that ties facts to a source rule:

```python
@dataclass(frozen=True, slots=True)
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
    """Build the nullability property with leaf facts grounded from manifest.

    Combines the shipping discoverers (``not_null`` tests, column nullable
    flag, native NOT NULL constraints) with any caller-supplied extras.
    """
    facts = collect_facts(
        manifest,
        discoverers=(*_default_nullability_discoverers, *extra_discoverers),
        name_to_source=name_to_source,
    )
    return Property(
        name="nullability",
        semiring=NullabilitySemiring(),
        source=source_rule_from_facts(
            facts,
            combine=NullabilitySemiring().times,
            default=Nullability.UNKNOWN,
        ),
        operators={...},
        aggregates={...},
        unknown_value=Nullability.UNKNOWN,
    )
```

Properties without facts (`where_provenance` today; `aggregation_depth` for the foreseeable future) keep using a constant source rule. The substrate does not impose facts on properties that don't want them.

## Soundness contract

Same posture as uniqueness facts, restated for the column level:

1. **Discoverer correctness is a hard guarantee.** A discoverer that emits a fact the manifest does not support is a substrate-level bug. PBT covers each shipping discoverer.
2. **Absence is silence, not a default fact.** A column the manifest does not cover is absent from the fact store. `source_rule_from_facts` returns the property default for it. Detectors interpret the default as "we don't know."
3. **Conditional facts are captured but not activated yet.** A `not_null` test with a `where` filter, or a `dbt_utils.accepted_range` scoped to `country = 'US'`, produces a fact-shaped object with the predicate attached, but the standard `source_rule_from_facts` ignores conditional facts. Activation is a follow-up that picks an activation rule consistent with the rule [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md) commits to.
4. **Sources for a fact compose by precedence, not by guess.** When two discoverers produce different facts on the same column for the same axis (a `not_null` test says NON_NULL, a column `nullable: true` flag says NULLABLE), the property's `combine` rule decides. The default is to surface a build-time diagnostic and pick the higher-trust source per `FactSource`'s rank; a property can override.
5. **Facts cross model boundaries only through propagation.** Facts seed leaves on the model that declared them. The propagator carries the K value to downstream models through the lineage graph. There is no facility for "this fact applies to all downstream models" outside the propagator's reach.

## Failure modes

- **Manifest missing the data a discoverer wants.** The discoverer yields nothing for that column. Property source rule returns default. Diagnostic: the audit report counts how many columns each discoverer found facts for, so reviewers see when a manifest is sparse.
- **Conflicting facts within a single source.** A test and a contract on the same column with incompatible claims is a manifest bug; the audit surfaces it as a `BuildIssue` and the property's `combine` returns its preferred value. The audit never silently picks one and continues.
- **Discoverer raises.** Caught at the discovery layer, surfaced as a `BuildIssue` for the affected model, and the discoverer's facts for that model are dropped. Other discoverers proceed.

## What this does not cover

- **Activation of conditional facts.** See [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md); the same activation question applies here, and the same deferral applies until a concrete consumer asks.
- **World enumeration over flag values.** Belongs to the flag system. This module supplies leaf values inside a world; the flag layer chooses the world.
- **Cross-package fact inference.** Facts declared in a dbt package and consumed by a downstream package that does not import it. Same scope cut as [`var-inference-spec.md`](./var-inference-spec.md).
- **Runtime facts from the warehouse.** Reading `INFORMATION_SCHEMA.COLUMNS` for an actually-deployed column's nullability, types via the adapter. Useful but distinct from manifest-derived static facts; lands when an adapter-aware fact source is requested.
- **Inference from SQL.** A column projected as `COALESCE(x, 0)` could ground a nullability fact even without a `not_null` test. That work belongs in the property's operator rules (the propagator already does it). Discoverers consult the manifest, not the SQL.

## Sequencing

1. The data model: `ColumnFact[K]`, `FactSource`, `FactsByColumn`, `FactDiscoverer`, `source_rule_from_facts`, `collect_facts`. Discoverer-extensible from the start.
2. Nullability discoverers (`not_null` test, column `nullable`, native `NOT NULL`), nullability promoted to a production property. Closes the source-rule piece of [`#26`](https://github.com/dvryaboy/dblect/issues/26).
3. Type discoverer (column `data_type`). First property that consumes it is downstream of the semantic-types substrate.
4. Accepted-values and range discoverers. Power the first wave of developer-defined refinements (`PositiveInt`, `Country`, …).
5. Config discoverer with concrete per-key fact mappings as detectors adopt them.
6. Var discoverer wired to single-value flag assignments. Bridge to the flag world enumerator.

Steps 1 and 2 are one PR. The rest are independent and can land in any order driven by the consumer.

## Testing

- **Per-discoverer PBT.** Generate manifests with random combinations of column-level metadata; assert each discoverer's facts are a function of the manifest input it documents, never invent claims, and never drop claims it should produce.
- **Combine-rule PBT.** For each property's chosen `combine`, assert associativity and commutativity hold on the discovered facts so reordering discoverers does not change the source rule.
- **Soundness round-trip on jaffle.** For nullability with manifest-derived facts, assert that every column the audit annotates `NON_NULL` is either a column with a `not_null` test or a column whose projection expression makes it `NON_NULL` (e.g., `COALESCE(x, 0)`). Catches a discoverer that over-claims and a propagator rule that under-checks at the same time.
- **Conditional-fact capture.** A `not_null` test with a `where` filter must produce a fact with the predicate attached and the standard source rule must ignore it. Pins the deferred-activation contract.

## References

- The substrate this layers on: [`column-level-lineage.md`](./column-level-lineage.md).
- The model-level facts precedent: [`../../src/dblect/uniqueness/facts.py`](../../src/dblect/uniqueness/facts.py) and the soundness posture in [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- The long-term consumer of var-derived facts: [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md) and the discovery side, [`var-inference-spec.md`](./var-inference-spec.md).
- Issue [`#26`](https://github.com/dvryaboy/dblect/issues/26): promotes the demo nullability + aggregation-depth properties; the source-rule piece is what this module unblocks.
