# Lineage facts: grounding annotations from declarations

Status: design
Audience: engineers working on the lineage substrate, on a `Property[K]` that needs values from manifest declarations or developer assertions, or on the flag system that will eventually feed configuration values into property propagation.

## Motivation

The substrate from [`column-level-lineage.md`](./column-level-lineage.md) gives every `Property[K]` a graph to propagate through. It does not say where K-values enter the graph. Each property's `source: Callable[[ColumnRef], K]` rule has to invent its own grounding, and today the demo properties hard-code constants (`UNKNOWN` for nullability, `0` for aggregation depth) because there is no shared way to read `not_null` tests, declared column types, native constraints, candidate keys, or developer refinement declarations off the manifest.

The win this unlocks is letting a developer declare a refinement, like `Currency(USD)` on `B.amount` or `unique(customer_id, region)` on `dim_customer`, on the model where the meaning lives. The framework then propagates the claim downstream as the contract callers can rely on, and checks it against the SQL that produces the model from upstream. Without a facts module, every property that wants such grounding reimplements manifest plumbing, picks its own precedence rules, and tests its own discovery code.

A `lineage.facts` module turns this into a substrate concern. It carries the uniqueness layer's posture (rock-solid claims, soundness over completeness, opportunistic detector consumption) but generalises across scopes: a fact can be about one column, a column combination, or a whole relation, and the substrate treats them uniformly. Today's `uniqueness.facts` migrates onto this substrate as a specific `Property[K]`; the same module is the bridge to the flag system when a config or var carries a refinement.

## What a fact is

A **lineage fact** is a typed claim about a *scope* on a relation, under one property, with provenance. The scope is a (possibly empty) set of columns on one model or source:

| Scope cardinality | Kind                 | Examples                                                        |
|-------------------|----------------------|-----------------------------------------------------------------|
| 0                 | Relation-level fact  | Row-count interval, materialization mode                        |
| 1                 | Column-level fact    | Nullability, type, `PositiveInt`, currency                      |
| ≥ 2               | Combination-level fact | Candidate key, sort-order tuple, top-level group-by set       |

The propagator's behaviour at a scope depends on whether the scope has a derivation in the lineage graph:

- **Anchoring.** No derivation (source or seed columns; the source relation itself). The fact is the only input the propagator has.
- **Asserted.** The scope is derived (a model output column, a model's candidate key emerging from a SELECT). The fact is a developer or contract claim about what that derivation produces. The propagator uses it forward and checks it against the upstream.

The contract is the one the uniqueness layer holds today: facts must be rock-solid because downstream detectors silently rely on them. A wrong fact produces a wrong annotation produces a false-positive finding. An absent fact produces a missing annotation produces a silent skip. The audit is louder when it knows and quieter when it does not.

## Position relative to existing substrate

```
   audit detectors
          ↓
   Property[K] + propagate(graph, prop)
          ↓
   lineage.facts          ←  uniqueness migrates onto this (see "Uniqueness migration")
          ↓
   Manifest (Node, Column, DbtTestMetadata, ConstraintSpec, …)
```

The existing `uniqueness/facts.py` lives in its own layer because its facts are model-keyed and its propagation runs an ad-hoc walker. Both fall out as a `Property[K]` once the substrate supports combination-scoped facts. Until that migration lands, `uniqueness/facts.py` continues to back the uniqueness detectors; the new substrate runs in parallel.

## Data model

```python
from typing import Generic, Iterable, Mapping, Protocol, TypeVar
from dataclasses import dataclass
from enum import StrEnum

from dblect.lineage.graph import ColumnRef, SourceRef

K = TypeVar("K")


@dataclass(frozen=True, slots=True)
class Scope:
    """A fact's subject. Cardinality of ``columns`` is the kind."""

    relation: SourceRef
    columns:  frozenset[ColumnRef] = frozenset()


class FactSource(StrEnum):
    """Where a fact came from. Ranked by trust for tie-breaking."""

    NATIVE_CONSTRAINT      = "native_constraint"      # dbt 1.5+ constraints
    MODEL_CONTRACT         = "model_contract"         # ModelContract declarations
    DBT_GENERIC_TEST       = "dbt_generic_test"       # not_null, unique, accepted_values, …
    DBT_UTILS_TEST         = "dbt_utils_test"         # unique_combination_of_columns, accepted_range, …
    COLUMN_METADATA        = "column_metadata"        # data_type, nullable in yaml
    DBT_CONFIG             = "dbt_config"             # node.config[...] keys
    DBT_VAR                = "dbt_var"                # vars from dbt_project.yml
    USER_ASSERTED          = "user_asserted"          # explicit declaration in audit config


@dataclass(frozen=True, slots=True)
class Fact(Generic[K]):
    """One claim about one scope under one property."""

    scope:  Scope
    value:  K
    source: FactSource
    detail: str | None = None

    @classmethod
    def column(cls, col: ColumnRef, value: K, source: FactSource, detail: str | None = None) -> "Fact[K]":
        return cls(Scope(col.source, frozenset({col})), value, source, detail)

    @classmethod
    def combination(cls, cols: Iterable[ColumnRef], value: K, source: FactSource, detail: str | None = None) -> "Fact[K]":
        cs = frozenset(cols)
        relations = {c.source for c in cs}
        if len(relations) != 1:
            raise ValueError("combination must be on one relation")
        return cls(Scope(next(iter(relations)), cs), value, source, detail)

    @classmethod
    def relation(cls, ref: SourceRef, value: K, source: FactSource, detail: str | None = None) -> "Fact[K]":
        return cls(Scope(ref, frozenset()), value, source, detail)


FactsByScope = Mapping[Scope, tuple[Fact[K], ...]]


class FactDiscoverer(Protocol[K]):
    """Reads the manifest, yields ``Fact[K]``s for any scope it can ground.

    Pure: same manifest in, same facts out, no mutable state.
    """

    def discover(
        self,
        manifest: "Manifest",
        *,
        name_to_source: Mapping[str, SourceRef],
    ) -> Iterable[Fact[K]]: ...
```

`fact_lookup` folds multiple facts at a scope via a property-supplied combine rule and returns `None` when no fact applies:

```python
def fact_lookup(
    facts: FactsByScope[K],
    *,
    combine: Callable[[K, K], K],
) -> Callable[[Scope], K | None]:
    """``None`` means the propagator should fall through to its walk.

    ``combine`` is the property's choice for "two facts at one scope."
    Lattice K's use ``semiring.times`` (every claim holds). Type-like K's
    where disagreement is a hard error use a strict combiner that raises.
    Accumulating axes use a custom fold.
    """
    def lookup(scope: Scope) -> K | None:
        bucket = facts.get(scope)
        if not bucket:
            return None
        return reduce(combine, (f.value for f in bucket))
    return lookup
```

## Discovery rules

A discoverer per axis. The substrate ships discoverers for the axes production properties need first; user properties register their own.

| Axis                | Manifest input                                                | Fact scope       |
|---------------------|---------------------------------------------------------------|------------------|
| Nullability         | `not_null` tests, column `nullable` flag, native `NOT NULL`   | column           |
| Type                | column `data_type`                                            | column           |
| Accepted-values     | `accepted_values` test, native `CHECK ... IN (...)`           | column           |
| Range               | `dbt_utils.accepted_range`, native `CHECK x BETWEEN ...`      | column           |
| Tags / meta         | column-level `tags` and `meta` keys                           | column           |
| Candidate key       | `unique` test (singleton), `unique_combination_of_columns`, native `PRIMARY KEY` / `UNIQUE` | column or combination |
| Row-count interval  | `dbt_utils.expression_is_true` shaped as a count assertion    | relation         |

Anchoring facts come from declarations on a source node; asserted facts come from declarations on a model (column-level tests, model contracts, refinement-type bindings when the types layer lands). The discoverer does not distinguish: a `Fact[K]` keyed on a model column's scope lands on that model column.

Two axes are forward-looking:

- **Config-derived facts.** A `dbt_config` discoverer reads `node.config` keys a property is interested in (`materialized`, `incremental_strategy`, …) and produces relation-level facts. Plumbing lands with this module; per-key fact mappings land as concrete refinements adopt them.
- **Var-derived facts.** A `dbt_var` discoverer produces facts where a refinement type's `affects` clause has a single-value mapping. Multi-value mappings remain in the world-enumeration scope of the flag system. World enumeration picks an assignment, the var discoverer produces facts under it, the lookup consumes them.

A discoverer is pure and total within its axis. Total means: every scope the discoverer claims authority over either gets a fact or is silently skipped. No `value=unknown` facts pretending to be claims.

## Property integration

A property's `K` is a type, the propagator does inference over it, and a fact is a type annotation at a `Scope`. At each scope the propagator has up to two inputs:

- The **inferred K**, from walking the upstream expression for column-scoped properties, or the relation-algebra structure for combination/relation-scoped properties. Absent for sources and seeds.
- The **declared K**, from `fact_lookup`. Absent when no fact applies.

| Inferred | Declared | Output K          | Behaviour                                                       |
|----------|----------|-------------------|-----------------------------------------------------------------|
| absent   | absent   | `prop.default()`  | No information.                                                 |
| present  | absent   | inferred          | Standard propagation.                                           |
| absent   | present  | declared          | Declaration anchors the scope.                                  |
| present  | present  | declared          | Subject to `consistent(declared, inferred)`.                    |

The property declares `consistent: Callable[[K, K], bool]`. It expresses "the inferred K is at least as specific as what the declaration committed to." When it holds, the declared K is the scope's annotation downstream (the declaration is the contract callers built against). When it fails, the audit surfaces a finding; downstream still sees the declared K so one upstream regression does not blank analysis of every consumer.

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
        scope=Scope.COLUMN,
        semiring=NullabilitySemiring(),
        facts=fact_lookup(facts, combine=NullabilitySemiring().times),
        consistent=_nullability_consistent,
        operators={...},
        aggregates={...},
        unknown_value=Nullability.UNKNOWN,
    )
```

`Property[K]` gains a `scope` declaration (`COLUMN`, `COMBINATION`, `RELATION`) and two callables: `facts` (the lookup) and `consistent` (the subtyping check). The scope tells the propagator which walk to do (per-column projection vs relation-algebra); `facts` and `consistent` are uniform across scopes. Properties that don't opt into facts supply a `facts` callable that returns `None` everywhere.

## Soundness contract

1. **Discoverer correctness is a hard guarantee.** A discoverer that emits a fact the manifest does not support is a substrate bug. PBT covers each shipping discoverer.
2. **Absence is silence.** A scope the manifest does not cover is absent from the fact store. The propagator returns the property default. Detectors interpret it as "we don't know."
3. **Conditional facts are captured but not activated.** A `not_null` or `unique` with a `where` filter produces a fact with the predicate attached, but `fact_lookup` ignores it. Activation follows the rule [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md) eventually commits to.
4. **Two discoverers, one scope.** When two discoverers produce different facts for the same axis at the same scope, the property's `combine` rule decides. The default is to surface a build-time diagnostic and pick the higher-trust source per `FactSource`'s rank.
5. **Facts cross model boundaries only through propagation.** Facts apply to the scope on the model that declared them. The propagator carries the K value downstream through the lineage graph.
6. **Asserted facts are checked.** A fact on a derived scope runs through `consistent` against the inferred K. A mismatch is a finding. When the upstream is unanalysable, the inferred K is the property default, the lattice top, and `consistent` passes vacuously.

## Failure modes

- **Manifest sparse on a discoverer's axis.** Discoverer yields nothing for that scope. Propagator returns the property default. The audit report counts how many scopes each discoverer grounded so reviewers see when a manifest is sparse.
- **Conflicting facts within a single source.** A test and a contract at the same scope with incompatible claims is a manifest bug; the audit surfaces it as a `BuildIssue` and `combine` returns its preferred value.
- **Discoverer raises.** Caught at the discovery layer, surfaced as a `BuildIssue` for the affected model, its facts for that model are dropped. Other discoverers proceed.

## Uniqueness migration

The existing `uniqueness/facts.py` is the worked example for combination-scoped facts. Migrating it onto this substrate looks like:

**Encoding.** Uniqueness becomes a `Property[CandidateKeySet]` with combination-scope facts. The K-relations encoding from [`column-level-lineage.md`](./column-level-lineage.md) (`K = frozenset[frozenset[ColumnRef]]`, set of candidate key sets) supplies the algebra. Operator transfers come straight from the literature: `plus` intersects branch key sets (`UNION ALL` retains a key only if both arms carry it); `times` unions key sets across sides (`JOIN` combines keys subject to join-condition coverage); `DISTINCT` and top-level `GROUP BY` introduce the projection set as a key.

**Discoverers.**

| Manifest input                                 | Fact                                                          |
|------------------------------------------------|---------------------------------------------------------------|
| `unique` test on column `c`                    | `Fact.column(c, value=…)` saying `{c}` is a candidate key     |
| `unique_combination_of_columns(c1, c2, …)`     | `Fact.combination({c1, c2, …}, value=…)`                      |
| Native `PRIMARY KEY (c1, c2)` constraint       | `Fact.combination({c1, c2}, value=…)`                         |
| Native column-level `UNIQUE`                   | `Fact.column(c, value=…)`                                     |

The same constants (`NATIVE_CONSTRAINT`, `DBT_GENERIC_TEST`, `DBT_UTILS_TEST`) the substrate already has rank these by trust the way `uniqueness/facts.py` does today.

**What goes away.**

- The model-keyed `UniquenessFact` dataclass. Combination-scope `Fact[K]` carries the same information at the substrate's standard shape.
- `uniqueness/propagation.py`'s separate walker. Its rules (`DISTINCT`-introduces-keys, `JOIN`-unions-keys, CTE pass-through) become operator transfers on the uniqueness property's `operators`/`aggregates`. The propagator from `column-level-lineage.md` walks them; one engine instead of two.
- The multi-source bail in `uniqueness/detector.py` (issue [`#16`](https://github.com/dvryaboy/dblect/issues/16)). The substrate propagates candidate keys across model boundaries naturally because facts on a JOIN's upstream propagate through `times` to the JOIN's output. The "single ref'd model" special case stops being a special case.
- Per-fact `derived_from` chains as a separate field. The propagator's recursion through `graph.edges` reconstructs them on demand; the audit report exposes a "trace this annotation to its grounding facts" helper.
- The `_build_name_to_uid` and `_parse_models` plumbing in `uniqueness/facts.py`. The substrate's `collect_facts` and the lineage builder cover both.

**What requires care.**

- `DISTINCT` and top-level `GROUP BY` produce relation-output facts (the projection list is a key on the model output). These look like combination-scope facts whose scope is the entire model. The operator rule writes them at the model-output `Scope`; downstream models read them via `fact_lookup` on the same scope.
- The K-relations literature is most natural at the row level; lifting to per-scope annotations means a property writer has to think about whether a transfer rule reads "annotations on the upstream relation" vs "annotations on individual upstream columns." For uniqueness, the operator rules already documented in `column-level-lineage.md` get this right; new combination-scoped properties should reuse the same pattern.
- Conditional uniqueness facts (`unique` test with a `where` filter) carry over with the same deferral as [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md). The substrate captures the predicate; activation lands when a concrete consumer asks.

**Sequencing.** The migration is its own PR after the substrate lands with nullability. Existing `uniqueness/facts.py` keeps backing the detectors while the new path is built and validated. A "both paths agree on jaffle" test pins parity for the cut-over. After cut-over, `uniqueness/facts.py` collapses to a thin compatibility shim or retires entirely.

## What this does not cover

- **Activation of conditional facts.** See [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- **World enumeration over flag values.** Belongs to the flag system. This module supplies values inside a world; the flag layer chooses the world.
- **Cross-package fact inference.** Facts declared in a dbt package and consumed by a downstream package that does not import it. Same scope cut as [`var-inference-spec.md`](./var-inference-spec.md).
- **Runtime facts from the warehouse.** `INFORMATION_SCHEMA.COLUMNS` or adapter-side metadata. Lands when an adapter-aware fact source is requested.
- **Inference from SQL.** A column projected as `COALESCE(x, 0)` could ground a nullability fact even without a `not_null` test. That work lives in the property's operator rules.

## Sequencing

1. The data model: `Scope`, `Fact[K]`, `FactSource`, `FactsByScope`, `FactDiscoverer`, `fact_lookup`, `collect_facts`. `Property[K]` gains `scope`, `facts`, `consistent`. Propagator change: consult `facts` at every scope and run `consistent` whenever both an inferred and a declared K are present.
2. Nullability discoverers (`not_null` test, column `nullable`, native `NOT NULL`), nullability promoted to a production property with `consistent` set to the nullability precision order. Closes the source-rule piece of [`#26`](https://github.com/dvryaboy/dblect/issues/26).
3. Uniqueness migration (own PR; see "Uniqueness migration"). Retires `uniqueness/facts.py` and `uniqueness/propagation.py`, closes [`#16`](https://github.com/dvryaboy/dblect/issues/16).
4. Type discoverer (column `data_type`). First consumer is the semantic-types substrate.
5. Accepted-values and range discoverers. Power the first wave of developer-defined refinements (`PositiveInt`, `Country`, …).
6. Config discoverer with concrete per-key fact mappings as detectors adopt them.
7. Var discoverer wired to single-value flag assignments. Bridge to the flag world enumerator.

Steps 1 and 2 are one PR. The rest are independent and land driven by the consumer.

## Testing

- **Per-discoverer PBT.** Generate manifests with random combinations of column-level and combination-level metadata; assert each discoverer's facts are a function of its documented manifest input, never invent claims, never drop ones they should produce.
- **Combine-rule PBT.** Associativity and commutativity on each property's chosen `combine`, so reordering discoverers does not change the lookup.
- **Soundness round-trip on jaffle.** For nullability, every column the audit annotates `NON_NULL` either has a `not_null` test or has a projection expression that makes it `NON_NULL` (`COALESCE(x, 0)`). For uniqueness after migration, every candidate key the audit annotates is either grounded by a discoverer or derived from upstream keys through a documented operator rule.
- **`consistent` PBT.** Reflexivity (`consistent(k, k)`), and `consistent(declared, prop.default())` for every K (so opaque upstreams never produce findings).
- **Asserted-fact end-to-end.** A `not_null` declaration on `B.amount` with `NULLABLE` upstream surfaces a finding; the same declaration with `NON_NULL` or `UNKNOWN` upstream propagates to downstream models without a finding. The analogous test for a candidate-key declaration on a derived model.
- **Uniqueness parity.** Before retiring `uniqueness/facts.py`, run both the old and new paths against the jaffle fixture and assert agreement on every model's candidate keys.
- **Conditional-fact capture.** A `not_null` or `unique` test with a `where` filter produces a fact with the predicate attached; `fact_lookup` ignores it.

## References

- The substrate this layers on: [`column-level-lineage.md`](./column-level-lineage.md), including the K-relations encoding for uniqueness this migration uses.
- The current uniqueness facts module: [`../../src/dblect/uniqueness/facts.py`](../../src/dblect/uniqueness/facts.py) and the deferred-activation posture in [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- The long-term consumer of var-derived facts: [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md), and the discovery side, [`var-inference-spec.md`](./var-inference-spec.md).
- Issue [`#26`](https://github.com/dvryaboy/dblect/issues/26): promotes the demo nullability + aggregation-depth properties; the source-rule piece is what this module unblocks.
- Issue [`#16`](https://github.com/dvryaboy/dblect/issues/16): multi-source uniqueness detectors consume the substrate; falls out of the uniqueness migration.
