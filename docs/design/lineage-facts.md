# Lineage facts: grounding annotations from declarations

Status: design
Audience: engineers working on the lineage substrate, on a `Property[K]` that needs values from manifest declarations or developer assertions, or on the flag system that will eventually feed configuration values into propagation. The first half doubles as a tutorial on how property propagation works, so a reader new to the substrate can start here.

## Motivation

The substrate from [`column-level-lineage.md`](./column-level-lineage.md) gives every property a graph to propagate through. It does not say where values *enter* the graph. Each property has to invent its own grounding, and today the demo properties hard-code constants (`UNKNOWN` for nullability, `0` for aggregation depth) because there is no shared way to read `not_null` tests, declared column types, native constraints, candidate keys, or developer refinement declarations off the manifest.

The capability this unlocks is letting a developer declare a refinement, like `RevenueNet` on `fct_orders.order_total` or a candidate key on `dim_customer`, on the model where the meaning lives. The framework then propagates the claim downstream as the contract callers can rely on, and checks it against the SQL that produces the model from upstream. Without a shared facts module, every property that wants such grounding reimplements manifest plumbing, picks its own precedence rules, and tests its own discovery code.

A `lineage.facts` module turns this into a substrate concern. It carries the uniqueness layer's posture (rock-solid claims, soundness over completeness, opportunistic detector consumption) and generalises it: a fact can be about one column or a whole relation, and the substrate treats both uniformly. Today's `uniqueness/facts.py` migrates onto this substrate as one `Property[K]`, and the same module is the bridge to the flag system when a config or var carries a refinement.

This module is also the convergence point for the three authoring channels the rest of the design relies on (dbt tests and constraints, `meta.dblect.*` blocks in `schema.yml`, and the Python `SemanticType` / `ModelContract` declarations described in [`dblect_technical_intro.md`](./dblect_technical_intro.md)). A goal of this doc is to show that the substrate carries all three without exposing any of its machinery to the people writing those declarations.

## How propagation works

A short tour of the engine the facts module feeds, building on the provenance-semiring framework (Green, Karvounarakis, Tannen 2007) and the functional-dependency propagation tradition (Abiteboul, Hull, Vianu).

A **property** is a value type `K` plus rules for moving `K` through SQL. `K` is a small lattice: a set of values ordered by *precision*, where a more precise value commits to more about the data. Nullability is the three-point lattice `NON_NULL ⊑ NULLABLE ⊑ UNKNOWN`, read "non-null is more precise than nullable, which is more precise than no information." Uniqueness is the lattice of candidate-key sets `frozenset[frozenset[ColumnRef]]`, where knowing more keys is more precise. The lattice carries a commutative semiring `(K, +, ×, 0, 1)`:

- `+` reconciles values at *confluence* points (a `UNION ALL`, or several branches feeding one downstream column). For nullability it is the lattice join (if either branch can be null, the union can be null).
- `×` combines values at *cross* points (the implicit cross product under every `JOIN`) and folds a multi-input expression into one value.
- `0` and `1` are the absorbing and neutral elements.

The **propagator** walks the lineage graph once per property and produces an annotation for every node. At a node with a derivation it reduces the node's expression to a single `K` by recursing into upstream nodes and applying the property's per-operator transfer rules; at a node with no derivation (a source or a seed) it reads the starting value from facts. Aggregates use the semimodule extension (Amsterdamer, Deutch, Tannen 2011): each aggregate function is a `K → K` transfer keyed on the sqlglot expression subclass.

Two points matter for everything below:

- **One engine, many properties, one pass each.** Adding a property is adding a `Property[K]`, never a new pass. Properties are independent: nullability propagation never reads uniqueness annotations. They share the graph, never the annotations. "Two lattices" elsewhere in the design (structural versus user-domain) is a difference in where transfer rules come from, not two engines.
- **Annotations degrade, they never lie.** When sqlglot cannot resolve a column the propagator emits the property's `UNKNOWN`-shaped default, and detectors read that as "we don't know" and stay silent. A wrong annotation would produce a wrong finding, so the contract is silence over guessing.

## Transfer rules: framework-proven and user-supplied

Properties differ by where their *transfer rules* come from. This is read off the catalog, not stored on the property and never branched on by the engine.

- **Framework transfers** are theorems about SQL semantics, true in every project: a `JOIN` multiplies cardinality, `DISTINCT` introduces a key, `COALESCE(x, 0)` is non-null. Nullability, uniqueness, cardinality, grain, and ordering are built from these, in a closed catalog the framework proves once.
- **User transfers** rest on declared signatures: whether `revenue * 0.9` preserves tax inclusion is what the author meant, which the framework cannot derive. Currency, tax inclusion, gross/net, and the rest of the user-domain axes are built from these, in an open catalog users extend.

A property is *framework-owned* when all its transfers are framework ones. The split surfaces in only two places, neither an engine fork: a finding is reported as conditional on whatever it rests on, which the propagator's recursion already traces (the leaf facts and rules behind each annotation), and a framework-owned property may depend only on framework-owned ones, so a structural conclusion never rests on a user transfer. Leaf facts are assertions regardless (a `not_null` on a source is the author vouching), so a framework-owned guarantee reads "theorems given your declared facts" and a user-extended one adds "and given your signatures." Both tighten toward the most precise justified value, as "Validation and propagation" makes precise.

A property's transfer behaviour is indexed by relational operator, and most of it is forced by the semiring and lattice rather than chosen:

- **Filter / selection**: preserve. Forced.
- **Union**: the lattice join, the semiring `+` at a confluence. Forced.
- **Join**: each side preserves, combined across sides by the semiring `×`. Forced.
- **Scalar / projection**: preserve, transform, or clear. A genuine choice. An identity transfer (`Alias`, a bare `Column`) preserves and is where tightening happens; a declared map (a currency conversion, a `discount` or `tax` annotation) transforms it; an opaque scalar or a bare literal clears the value to `UNKNOWN`. A binary combine (`a + b`) preserves when both operands agree on the axis and clears when they do not: two committed-but-incompatible operands (tax-inclusive plus tax-exclusive) are a contradiction that raises a finding, while a committed operand combined with an unrefined one clears to `UNKNOWN` under the seam rule in "Validation and propagation." Clearing records its cause, an explicit opt-out or an absent annotation, the way a type checker separates an explicit `Any` from an implicit one.
- **Aggregation**: the aggregate transfer, whose behaviour is the measure's *combinability*. A genuine choice.

So a property chooses behaviour only at scalar transforms and at aggregation; the rest follows from the algebra.

The aggregate transfer asks whether a measure's meaning survives a `GROUP BY` or aggregate, and under what precondition. Three outcomes cover it: the refinement is **preserved** (a value-returning aggregate over a normal measure keeps its axes), **preserved under coherence** (it survives only where named columns are constant in the aggregation scope, the currency-coherence case, which is where the transfer reads a functional dependency through `depends_on`), or **cleared** (no aggregate preserves it, as for a ratio). Coherence is the only place the aggregate transfer reads another property. The user-land vocabulary that compiles to these outcomes lives in the types layer; the v1 surface is a coherence declaration (`within=<cols>`) plus a flag for measures that never aggregate.

**Properties can read one another, in dependency order.** Most properties propagate alone: nullability never consults uniqueness. A few need another property's annotations to compute their own transfers, and two cases carry the design.

*Cardinality reads uniqueness.* To tell a fan-out join from a key-preserving one, the cardinality transfer at a `JOIN` asks whether the join key is unique on the other side. That answer is the uniqueness property's annotation, read at the join node. Both are framework-owned.

*A user-defined money type reads a functional dependency.* Currency is not a framework property. It is a refinement a developer declares, say a `Money` semantic type carrying a currency axis, which the types layer compiles to a user-extended property. Take `SELECT region, SUM(amount) AS total FROM orders GROUP BY region`, where `amount` is typed `Money`. Does `total` keep its currency? Only if every row folded into a group already shares one, that is, only if `region → currency` holds. The compiled currency transfer reads that functional dependency to decide: where it holds, currency is preserved; where it does not, the sum mixes currencies and the axis clears to `UNKNOWN`. This is the allowed direction, a user-extended property reading a framework-owned one; the reverse is forbidden.

A property names the properties its transfers read in `depends_on`, and the propagator evaluates those first. A transfer reaches them only through a read-only `DepContext` that exposes exactly the declared dependencies' annotations, never a shared global map. So the edge is a wire, not a hint: it sets evaluation order and it is the sole channel for the read. A transfer that never declared an edge cannot read that annotation at all, so a missing edge fails at authoring time rather than silently reading stale state. This keeps the wiring between properties honest.

Honest wiring does not manufacture information. If no one ever typed `amount` as `Money` (or otherwise declared its currency), there is no currency refinement on that column, and the mixed-currency `SUM` above draws no finding. That is the substrate's posture (absence is silence), not a gap the channel could close.

Two invariants keep this sound. The `depends_on` graph is acyclic, so no pair of properties needs a joint fixpoint over a product lattice. And a framework-owned property never depends on a user-extended one, so a structural conclusion never becomes conditional on a user-supplied transfer.

The user never writes any of this. The `depends_on` edge for the example originates in a coherence declaration on the `Money` type (a measure that aggregates only where its currency column is constant, written `within="currency"`). The framework compiles that intent into the dependency and the transfer; the user-land vocabulary lives in the types layer.

## What a fact is

A **fact** is a typed claim about one node of the lineage graph, under one property, with provenance. A node is either a column or a relation, and those are the only two subjects a fact can have. Anything that looks multi-column is a relation fact whose *value* names the columns: a candidate key `{customer_id, region}` is the statement "this relation is unique on `{customer_id, region}`," so it attaches to the relation and the column set lives in the value, never in the address.

The propagator's behaviour at a node depends on whether the node has a derivation:

- **Anchoring.** No derivation (a source or seed column, or the source relation itself). The fact is the only input the propagator has.
- **Asserted.** The node is derived (a model output column, a model's candidate key emerging from a `SELECT`). The fact is a developer or contract claim about what the derivation produces. The propagator uses it forward and checks it against the upstream.

The contract is the uniqueness layer's: facts must be rock-solid because detectors silently rely on them. A wrong fact produces a wrong annotation produces a false-positive finding. An absent fact produces a missing annotation produces a silent skip. The audit is louder when it knows and quieter when it does not.

## Position relative to existing substrate

```
   audit detectors
          ↓
   Property[K] + propagate(graph, prop)
          ↓
   lineage.facts          ←  uniqueness migrates onto this (see "Uniqueness migration")
          ↓
   Manifest + dblect declarations (Node, Column, DbtTestMetadata, ConstraintSpec, SemanticType, …)
```

The existing `uniqueness/facts.py` lives in its own layer because its facts are model-keyed and its propagation runs an ad-hoc walker. Both fall out as a `Property[K]` once the substrate supports relation-scoped facts. Until that migration lands, `uniqueness/facts.py` keeps backing the uniqueness detectors and the new substrate runs in parallel.

## Data model

A fact's subject reuses the graph's own node identities, so there is one addressing scheme rather than a parallel one:

```python
from typing import Callable, Generic, Iterable, Mapping, Protocol, TypeVar
from dataclasses import dataclass
from enum import StrEnum
from functools import reduce

from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.semiring import Semiring

K = TypeVar("K")

# A fact's subject: a column node (ColumnRef is (SourceRef, column)) or a
# relation node (SourceRef). These two key spaces never collide, so the
# propagator's annotation map is keyed by whichever the property uses.
Scope = ColumnRef | SourceRef

# A flag-world assignment chosen by the flag layer: the value each enumerated
# flag takes in this propagation run. ``None`` on a fact means "holds in every
# world" (a native constraint, a test, a user assertion). A set value scopes the
# fact to that world. Defined by the flag system; opaque to the substrate.
WorldRef = Mapping[str, object]


class ScopeKind(StrEnum):
    COLUMN   = "column"   # propagator walks per-column projections
    RELATION = "relation" # propagator walks relation-algebra structure


class Channel(StrEnum):
    """Where a fact was authored: provenance, for tracing an annotation back to
    its grounding and for reporting. It carries no authority ordering. Two facts
    contend only when they claim the same axis at one scope, and the substrate
    resolves that without a channel rank (see "Resolving multiple facts at a
    scope"). Distinct from where a property's transfer rules come from, which is
    about rule ownership rather than where a leaf value came from."""

    NATIVE_CONSTRAINT = "native_constraint"  # dbt 1.5+ constraint on the model
    MODEL_CONTRACT    = "model_contract"     # dbt model-contract declaration
    DBT_GENERIC_TEST  = "dbt_generic_test"   # not_null, unique, accepted_values, …
    DBT_UTILS_TEST    = "dbt_utils_test"     # unique_combination_of_columns, accepted_range, …
    COLUMN_METADATA   = "column_metadata"    # data_type, nullable in schema.yml
    DBT_META          = "dbt_meta"           # meta.dblect.* blocks in schema.yml
    USER_ASSERTED     = "user_asserted"      # Python SemanticType / Field / ModelContract
    COMPILE_VALUE     = "compile_value"      # a value resolved at compile time (see CompileOrigin)


class CompileOrigin(StrEnum):
    """For a ``COMPILE_VALUE`` fact, how the value reached the manifest. A dbt
    ``var()`` or ``env_var()`` is statically enumerable, so the flag layer can
    explore worlds over it. A value computed by Jinja or Python (a macro running a
    warehouse query, an env-derived constant) is not, so the flag layer sees the
    single resolved value as one world. Reporting only: soundness comes from the
    world a fact is scoped to, never from this label."""

    DBT_VAR    = "dbt_var"     # var() from dbt_project.yml
    ENV_VAR    = "env_var"     # env_var()
    DBT_CONFIG = "dbt_config"  # node.config[...] key
    COMPUTED   = "computed"    # Jinja/Python substitution, possibly a DB call; opaque to enumeration


@dataclass(frozen=True, slots=True)
class Fact(Generic[K]):
    """One claim about one node under one property.

    ``world`` is the conditioning regime. ``None`` holds in every flag world; a
    set value holds in that world and is ground truth there, not a low-confidence
    guess to be outranked. This is what answers the var question: a compile-value
    fact is scoped to a world, never ranked beneath an unconditional one.

    ``channel`` is provenance only. ``origin`` is set exactly when the channel is
    ``COMPILE_VALUE``. ``enforced_on_write`` is set only on native-constraint
    facts and records whether the active adapter enforces the constraint on write;
    it is read by the unenforced-constraint finding, never by fact resolution."""

    scope:             Scope
    value:             K
    channel:           Channel
    world:             WorldRef | None = None
    origin:            CompileOrigin | None = None
    enforced_on_write: bool | None = None
    detail:            str | None = None

    @classmethod
    def column(
        cls, col: ColumnRef, value: K, channel: Channel, *,
        world: WorldRef | None = None,
        origin: CompileOrigin | None = None,
        enforced_on_write: bool | None = None,
        detail: str | None = None,
    ) -> "Fact[K]":
        return cls(col, value, channel, world, origin, enforced_on_write, detail)

    @classmethod
    def relation(
        cls, ref: SourceRef, value: K, channel: Channel, *,
        world: WorldRef | None = None,
        origin: CompileOrigin | None = None,
        enforced_on_write: bool | None = None,
        detail: str | None = None,
    ) -> "Fact[K]":
        # ``value`` carries the relation-level claim. For uniqueness it is the
        # set of candidate key sets; the key columns live here, not in ``scope``.
        return cls(ref, value, channel, world, origin, enforced_on_write, detail)


FactsByScope = Mapping[Scope, tuple[Fact[K], ...]]


class FactDiscoverer(Protocol[K]):
    """Reads the manifest and dblect declarations, yields facts for any node it
    can ground. Pure: same input in, same facts out, no mutable state."""

    def discover(
        self,
        manifest: "Manifest",
        *,
        name_to_source: Mapping[str, SourceRef],
    ) -> Iterable[Fact[K]]: ...
```

## Resolving multiple facts at a scope

Several discoverers can ground the same node. A property supplies a `merge` that folds two facts into one. It receives whole `Fact`s, not bare values, so it can read provenance, and it resolves agreement and contradiction without any global ordering over channels:

```python
FactMerge = Callable[[Fact[K], Fact[K]], Fact[K]]


def fact_lookup(facts: FactsByScope[K], *, merge: FactMerge[K]) -> Callable[[Scope], K | None]:
    """``None`` means the propagator should fall through to its walk."""
    def lookup(scope: Scope) -> K | None:
        bucket = facts.get(scope)
        if not bucket:
            return None
        return reduce(merge, bucket).value
    return lookup
```

The substrate ships two default merges, one per shape of property:

- **Alternative properties** (nullability, type, a user-domain axis): two facts compete on one axis. The merge takes the more precise value when one refines the other. A genuine contradiction (neither refines the other, a `nullable: true` column flag against a native `NOT NULL`) is a manifest bug: the merge raises a `BuildIssue`, keeps a deterministic choice so the run is reproducible, and marks downstream annotations provisional. It does not try to decide who is right from the channel, because the channel does not carry that information and a contradiction wants a human, not a silent winner.
- **Accumulating properties** (uniqueness): two facts are independent claims that both hold. The merge unions them (every declared key is a key). No ordering arises because more keys never contradict.

This is the two-step the earlier sketch described, now without a trust ladder: the lattice operation decides agreement, and a contradiction is surfaced rather than ranked away. Channel stays on the fact for tracing and reporting, not pressed into an authority order it cannot bear.

**Compile-value facts do not enter this at all.** The flag layer fixes one world per propagation run and the compile-value discoverers emit their facts under it, so every fact in a bucket shares that world. Within the run such a fact is an anchor like any other, on equal footing with a native constraint or a user assertion. A difference *between* worlds is the flag-world analysis ([`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md)), reported as "this contract holds under world A and fails under world B," never collapsed by a merge. This is the answer to "why would a dbt var be less authoritative than a native constraint": it is not. A var-derived value is scoped to a world rather than ranked beneath one, and inside its world it is ground truth by construction. Where a world-scoped value and an unconditional user assertion genuinely disagree on one axis (a contract claims `contains_tax=True` always, world B produces `False`), that disagreement is the finding the analysis exists to raise, not a tie for the merge to break.

## Discovery rules

A discoverer per axis. The substrate ships discoverers for the axes production properties need first; user properties register their own. A discoverer is pure and total within its axis: every node it claims authority over either gets a fact or is silently skipped. It never emits a `value=unknown` fact pretending to be a claim.

| Axis                | Manifest / declaration input                                   | Fact node        |
|---------------------|----------------------------------------------------------------|------------------|
| Nullability         | `not_null` test, column `nullable` flag, native `NOT NULL`     | column           |
| Type                | column `data_type`                                             | column           |
| Accepted-values     | `accepted_values` test, native `CHECK ... IN (...)`            | column           |
| Range               | `dbt_utils.accepted_range`, native `CHECK x BETWEEN ...`       | column           |
| Tags / meta         | column-level `tags` and `meta` keys                            | column           |
| Candidate key       | `unique` test, `unique_combination_of_columns`, native `PRIMARY KEY` / `UNIQUE` | relation |
| Row-count interval  | `dbt_utils.expression_is_true` shaped as a count assertion     | relation         |

Two discoverers are forward-looking, and the plumbing for them lands with this module even though their per-key mappings arrive with the consumers. Both emit `COMPILE_VALUE` facts scoped to the world the flag layer chose:

- **Config-derived facts.** A discoverer reads `node.config` keys a property cares about (`materialized`, `incremental_strategy`) and produces relation facts (`origin=DBT_CONFIG`).
- **Compile-resolved values.** A discoverer produces facts where a refinement type's `affects` clause has a single value under the chosen world. The value need not come from a dbt `var()`. An `env_var()`, or Jinja or Python that computes a value at compile time (including a macro that runs a warehouse query), reaches the manifest the same way and is the same kind of fact. Where the value is statically enumerable (`origin=DBT_VAR`, `ENV_VAR`) the flag layer enumerates worlds over it; where it is computed opaquely (`origin=COMPUTED`) the flag layer sees the single resolved value as one world, matching the inference-failure posture in [`var-inference-spec.md`](./var-inference-spec.md). Either way the fact is scoped to a world, not ranked.

### From declaration to fact

The point of the facts layer is that the three authoring channels all reduce to `Fact[K]`, and a developer writing a declaration never meets `Scope`, `merge`, or the framework/user transfer catalogs. The channels:

| What the developer writes | Channel | Becomes |
|---|---|---|
| `not_null` / `unique` test, native constraint, column `data_type` | dbt manifest | structural grounding fact (`DBT_GENERIC_TEST`, `NATIVE_CONSTRAINT`, `COLUMN_METADATA`) |
| `meta.dblect.*` in `schema.yml` | manifest meta (read-only in v1) | bridge fact (`DBT_META`) |
| `order_total: RevenueNet = Field(non_negative=True)` on a `ModelContract` | Python declaration registry | user-domain fact (`USER_ASSERTED`) |
| `SemanticFlag.affects` resolved under a world | flag world enumerator | `COMPILE_VALUE` fact scoped to that world |

A worked example, the user-domain channel. A developer writes the Pandera-shaped declaration described in the intro doc:

```python
class FctOrders(ModelContract):
    dbt_model = "marts.fct_orders"
    order_total: RevenueNet = Field(non_negative=True)
```

A discoverer reading the declaration registry yields:

```python
Fact.column(
    ColumnRef(SourceRef("model.shop.fct_orders"), "order_total"),
    value=RevenueNet,                 # the refinement the developer declared
    channel=Channel.USER_ASSERTED,
)
```

Nothing in that path requires the author to know a fact store exists. The structural channels work the same way against `not_null` tests and native constraints, and the flag channel against `affects` under a chosen world. This is the round-trip check that the substrate-author API can carry the end-user API: the Pandera-shaped surface produces facts, the facts feed propagation, and the propagation produces the boundary checks and findings the developer sees.

## Validation and propagation

At a node the propagator has up to two inputs:

- the **inferred** `K`, from walking the upstream expression (absent at sources and seeds), and
- the **declared** `K`, from `fact_lookup` (absent when no fact applies).

Two independent decisions follow, and the old design collapsed them into one. They are worth keeping apart.

**Validation** asks whether the inferred value honours the declared contract, via a `consistent` predicate. For a lattice-shaped `K` the default is derived from the precision order rather than hand-written per property:

```python
def subtype_consistent(semiring: Semiring[K], *, top: K) -> Callable[[K, K], bool]:
    """Default ``consistent`` for a lattice-shaped K.

    The declaration is honoured when the inferred value is opaque ("we don't
    know", the lattice top) or already at least as precise as the declaration.
    Precision is read off the semiring: ``inferred`` refines ``declared`` when
    meeting them leaves ``inferred`` unchanged.
    """
    def consistent(declared: K, inferred: K) -> bool:
        return inferred == top or semiring.times(declared, inferred) == inferred
    return consistent
```

A property overrides `consistent` only when `K` is equality-shaped (a user-domain enum where disagreement is a hard error, not a meet). For lattices it is derived, so it cannot drift from the semiring.

**Propagation** asks what value flows onward. The rule is uniform across framework-owned and user-extended properties: flow the most precise value the framework can justify, where "justify" means every composition step from the declared inputs to here is a transfer that preserves or combines the value (a theorem or a user annotation) rather than one that clears it. The key is that *declared* governs the contract boundary while the *flow* value governs internal precision:

| inferred | declared | consistent | flow value (to downstream) | boundary value (contract) | finding |
|----------|----------|------------|----------------------------|---------------------------|---------|
| absent   | absent   | —          | default (`UNKNOWN`)        | default                   | none |
| present  | absent   | —          | inferred                   | inferred                  | none |
| absent   | present  | —          | declared                   | declared                  | none (anchors a source) |
| `UNKNOWN`, opted out | present | yes (vacuous) | declared           | declared                  | none (opacity declared) |
| `UNKNOWN`, erased implicitly | present | yes (vacuous) | declared   | declared                  | strict mode: "guarantee unverified" (see seam rule) |
| refines declared | present | yes  | inferred                   | declared                  | soft "can tighten" if strictly more precise |
| conflicts | present | no         | declared (provisional)     | declared                  | hard finding |

The two rows that carry the design:

- **`refines declared` (the tightening row).** The SQL proves something at least as precise as the declaration, so the flow value is the inferred one. For a framework-owned property that is unconditional (a `COALESCE` makes the column non-null whatever the declaration said). For a user-extended property it tightens only through preserving transfers and aggregates whose combinability precondition holds, because those are the justified steps; an opaque transformation clears the value instead. Either way the *boundary* stays at the declared value, so a downstream consumer that built against the looser contract is unaffected, and the developer keeps the right to declare looseness deliberately at a published boundary. When the inferred value is *strictly* more precise than the declaration, the audit emits a soft "you can tighten this declaration, or confirm the looseness is intentional" finding. It is informational and suppressible, softer for user-extended properties where deliberate abstraction is common.
- **`conflicts` (the violation row).** The inferred value contradicts the contract. The audit raises a finding at the violation site, and propagation falls back to the declared value downstream. This is error recovery in the spirit of a type checker: once the error is reported, assume the declared type so one upstream regression does not blank analysis of every consumer. Annotations downstream of a violation are *provisional*, computed under a contract the SQL does not currently honour, and detectors may downgrade findings that rest on them.

The property bundle gains the pieces this needs, and a constructor hides them behind a single call:

```python
class DepContext(Protocol):
    """Read-only access to the annotations of a property's declared
    dependencies, supplied to its transfers. A transfer can read only the
    properties its ``Property.depends_on`` names, never an arbitrary global
    map, so a cross-property dependency is always visible in the type."""

    def annotation(self, prop: "PropertyId", scope: Scope) -> object | None: ...


# Transfers receive the dependency context. A property with no dependencies
# ignores it; the common case never touches another property's annotations.
OperatorTransfer  = Callable[["Expr", tuple[K, ...], DepContext], K]
AggregateTransfer = Callable[["exp.AggFunc", K, DepContext], K]


@dataclass(frozen=True, slots=True)
class Property(Generic[K]):
    name:       str
    scope_kind: ScopeKind
    semiring:   Semiring[K]
    operators:  Mapping[type["Expr"], OperatorTransfer[K]]
    aggregates: Mapping[type["exp.AggFunc"], AggregateTransfer[K]]
    facts:      Callable[[Scope], K | None]   # the lookup; returns None everywhere if the property opts out
    consistent: Callable[[K, K], bool]        # derived from the order for lattices; overridden for equality K
    # Other properties this one's transfers read (cardinality → uniqueness,
    # currency → functional dependency). The propagator evaluates dependencies
    # first. Invariants: the depends_on graph is acyclic, and a framework-owned
    # property depends only on framework-owned ones (a registration check).
    depends_on: tuple["PropertyId", ...] = ()
    unknown_value: K | None = None


def nullability_property(
    manifest: "Manifest",
    *,
    name_to_source: Mapping[str, SourceRef],
    extra_discoverers: tuple[FactDiscoverer[Nullability], ...] = (),
) -> Property[Nullability]:
    semiring = NullabilitySemiring()
    facts = collect_facts(
        manifest,
        discoverers=(*_default_nullability_discoverers, *extra_discoverers),
        name_to_source=name_to_source,
    )
    return Property(
        name="nullability",
        scope_kind=ScopeKind.COLUMN,
        semiring=semiring,
        operators={...},
        aggregates={...},
        facts=fact_lookup(facts, merge=alternative_merge(semiring)),
        consistent=subtype_consistent(semiring, top=Nullability.UNKNOWN),
        unknown_value=Nullability.UNKNOWN,
    )
```

The existing `Property.source: Callable[[ColumnRef], K]` is subsumed by `facts`: it is the anchoring branch, the value at a node with no derivation. The propagator consults `facts` at every node, runs `consistent` whenever both an inferred and a declared value are present, and dispatches its walk on `scope_kind`.

### Erasure at the typed/untyped seam

A refined value meeting an unrefined one is worth dwelling on, because that seam is where the highest-value bugs hide and where a partial adopter most wants a nudge. dblect follows the gradual-typing settlement here (Siek and Taha on gradual typing; the `Any` / implicit-`any` / `unknown` distinction in mypy and TypeScript; Wadler and Findler on blame). The principle is to separate an explicit opt-out from an absent annotation and treat them oppositely:

- An `UNKNOWN` the modeler *declared* (an `OpaqueEffect`, a column marked opaque) is the explicit `Any`. It flows silently, because the modeler took responsibility for it.
- An `UNKNOWN` that is merely *un-annotated* is the implicit one. Where it meets a declared refinement, the audit speaks up, the way `noImplicitAny` and `--warn-return-any` do. This diagnostic is on by default once a project has declared semantic types (the typed-critical-chain layer) and off at the zero-declaration layer, so the signal lands where the investment already is rather than at every incidental untyped touch.

The runtime layer is the blame-assigning cast at the seam: the static side notes the boundary and the generator probes whether the unrefined side actually respects the refined side's assumption.

The diagnostic is a fixed template, not synthesized prose. dblect cannot author domain narrative like "this mixes tax-inclusive and tax-exclusive amounts," because it does not know what a user-domain axis means. It fills slots from what it does have: the site, the operator, the two operand columns and their types, the axis that cleared, and the suppression path. The only domain-flavored text is a name the modeler chose, the type's own name and an optional one-line description carried on the declaration (the Pandera-shaped surface already has a docstring), with graceful fallback to the bare type and axis names when none is given. The two readings (annotate to match, or a real mismatch) and the suppression line are constant; everything else is a named slot. A realistic rendering:

> `orders.sql:12`: `total` combines `revenue` and `net_revenue` with `+`. `net_revenue` is `RevenueWithTax` but `revenue` carries no refinement on `contains_tax`, so the result drops it. Annotate `revenue` as `RevenueWithTax` if it qualifies, or treat this as a possible mismatch. To silence: mark `revenue` opaque, or disable `refinement-erased-at-seam` for this model.

## Soundness contract

1. **Discoverer correctness is a hard guarantee for the input it reads.** A discoverer that emits a fact its declaration does not support is a substrate bug. PBT covers each shipping discoverer. Whether the resulting *conclusion* is unconditional depends on what it rests on: a framework-owned property's propagated values are theorems given the declared inputs; a user-extended property's hold given the declared inputs and the composition signatures.
2. **Absence is silence.** A node the input does not cover is absent from the fact store. The propagator returns the property default. Detectors read it as "we don't know."
3. **Conditional facts are captured but not activated.** A `not_null` or `unique` with a `where` filter produces a fact with the predicate attached, and `fact_lookup` ignores it. Activation follows the rule [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md) eventually commits to.
4. **Conflicts at one scope are resolved and surfaced.** When two declarations disagree at one node, the property's `merge` decides (the lattice operation for agreement; a surfaced `BuildIssue` plus a deterministic provisional value for a genuine contradiction) and the audit emits the `BuildIssue`. It does not pick a winner from the channel.
5. **Facts cross model boundaries only through propagation.** A fact applies to the node on the model that declared it. The flow value carries downstream through the lineage graph; the boundary value gates cross-model contract checks.
6. **Asserted facts are checked, and the boundary is stable.** A fact on a derived node runs through `consistent` against the inferred value. A mismatch is a finding. The declared value remains the contract callers built against, and downstream-of-violation annotations are provisional.

## Trusting unenforced constraints

Framework transfers are theorems; leaf facts are not. The transfer rules (a `JOIN` multiplies cardinality, `DISTINCT` introduces a key) are proven once. The candidate key, foreign key, or `not_null` claim that *seeds* the propagation is an assertion the framework cannot verify, since it never reads source data. So a framework-owned property's guarantee is conditional: given the declared source facts, the propagated values are theorems. A constraint the warehouse declares but does not enforce is a leaf-fact risk, not a transfer-rule risk: it can make a propagated annotation wrong about the data while the rules that produced it stay sound. "Framework-proven" must not be read as "verified against data."

Many warehouses (Snowflake, BigQuery, Redshift, Databricks) treat `PRIMARY KEY`, `UNIQUE`, and `FOREIGN KEY` as informational. Some support a `RELY` form the optimiser trusts for rewrites without validating the data, the same conditional bet this substrate makes; others are documentation only. So whether a native constraint actually backs its claim is an adapter-and-constraint-kind question (Databricks enforces `CHECK` but not `PRIMARY KEY`, Snowflake enforces neither), captured by the `enforced_on_write` flag on a native-constraint fact. For dblect's purposes the gradient collapses to one question: is the claim checked against data by something that runs? A dbt `unique` test is, because the runtime layer runs it; an advisory `PRIMARY KEY` is not, whatever its prominence. This is why the channel carries no authority order: the signal that matters is whether a running guard exists, and that is read where it is needed rather than baked into a rank.

Two things follow:

- **Discoverers are adapter-aware about enforcement.** The native-constraint discoverer knows the active adapter and sets `enforced_on_write` on each fact it emits. This is descriptive provenance, not a merge input: fact resolution never reads it. The unenforced-constraint finding below does.
- **The runtime layer is the backstop, and the gap gets a finding.** The audit's empirical checks (a model's identified primary key is unique in output) and the generator intents named for these violations (Orphan, NullKey, Duplicate, Boundary) exist to test whether advisory constraints actually hold. The static layer trusts the declaration; the runtime layer probes it. Where a load-bearing structural annotation rests on a native constraint with `enforced_on_write=False` that no running test covers at the same scope, the audit emits a finding ("uniqueness on `dim_customer.id` rests on an advisory `PRIMARY KEY` and no `unique` test guards it; the downstream key annotation is unverified, add a test"), turning a silent assumption into an actionable recommendation.

## Failure modes

- **Manifest sparse on a discoverer's axis.** The discoverer yields nothing for that node, the propagator returns the default, and the audit report counts how many nodes each discoverer grounded so reviewers see when a manifest is sparse.
- **Conflicting facts within a single source.** A test and a contract at the same node with incompatible claims is a manifest bug; the audit surfaces a `BuildIssue` and `merge` keeps a deterministic provisional value rather than guessing a winner.
- **Discoverer raises.** Caught at the discovery layer, surfaced as a `BuildIssue` for the affected model, its facts for that model dropped, other discoverers proceeding.

## Uniqueness migration

The existing `uniqueness/facts.py` is the worked example for relation-scoped facts.

**Encoding.** Uniqueness becomes a `Property[CandidateKeySet]` with `scope_kind=RELATION`, built entirely from framework transfers. The K-relations encoding from [`column-level-lineage.md`](./column-level-lineage.md) (`K = frozenset[frozenset[ColumnRef]]`, the set of candidate key sets) supplies the algebra. The candidate key is the *value* at the relation node, never a column-set address. Operator transfers come from the literature: `plus` intersects branch key sets (`UNION ALL` retains a key only if both arms carry it), `times` unions key sets across sides (`JOIN` combines keys subject to join-condition coverage), and `DISTINCT` and top-level `GROUP BY` introduce the projection set as a key.

**Discoverers.** All of these produce relation facts whose value is a key set:

| Manifest input                                 | Fact                                                                |
|------------------------------------------------|---------------------------------------------------------------------|
| `unique` test on column `c`                    | `Fact.relation(model, value={{c}})`                                 |
| `unique_combination_of_columns(c1, c2, …)`     | `Fact.relation(model, value={{c1, c2, …}})`                         |
| Native `PRIMARY KEY (c1, c2)` constraint       | `Fact.relation(model, value={{c1, c2}})`                            |
| Native column-level `UNIQUE` on `c`            | `Fact.relation(model, value={{c}})`                                 |

The `merge` is the accumulating one: independent declared keys union, so no contradiction arises and no ordering is needed. `Channel` records which declaration each key came from for the trace, the role `_dedupe`/`_SOURCE_RANK` plays today, now without imposing a rank.

**What goes away.**

- The model-keyed `UniquenessFact` dataclass. A relation `Fact[K]` carries the same information at the substrate's standard shape.
- `uniqueness/propagation.py`'s separate walker. Its rules (`DISTINCT`-introduces-keys, `JOIN`-unions-keys, CTE pass-through) become operator transfers on the uniqueness property. One engine instead of two.
- The multi-source bail in `uniqueness/detector.py` (issue [`#16`](https://github.com/dvryaboy/dblect/issues/16)). Facts on a `JOIN`'s upstream propagate through `times` to the `JOIN`'s output, so the "single ref'd model" special case stops being a special case.
- Per-fact `derived_from` chains as a stored field. The propagator's recursion reconstructs them on demand, and the audit exposes a "trace this annotation to its grounding facts" helper.
- The `_build_name_to_uid` and `_parse_models` plumbing. `collect_facts` and the lineage builder cover both.

**What requires care.** The relation-algebra walk is new substrate. The K-relations literature is most natural at the row level, and lifting to per-node annotations means a transfer rule has to be clear about whether it reads the upstream relation's annotation or the upstream columns' annotations. The operator rules in `column-level-lineage.md` get this right for uniqueness; new relation-scoped properties should reuse the pattern. Conditional uniqueness facts (a `unique` test with a `where` filter) carry over with the same deferral as [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md): the substrate captures the predicate, activation lands when a concrete consumer asks.

**Sequencing.** The migration is its own change after the substrate lands with nullability. Existing `uniqueness/facts.py` keeps backing the detectors while the new path is built and validated, a "both paths agree on jaffle" test pins parity for the cut-over, and after cut-over `uniqueness/facts.py` collapses to a thin shim or retires.

## What this does not cover

- **Activation of conditional facts.** See [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- **World enumeration over flag values.** Belongs to the flag system. This module supplies values inside a world; the flag layer chooses the world.
- **Cross-package fact inference.** Facts declared in a dbt package and consumed by a downstream package that does not import it. Same scope cut as [`var-inference-spec.md`](./var-inference-spec.md).
- **Runtime facts from the warehouse.** `INFORMATION_SCHEMA.COLUMNS` or adapter-side metadata. Lands when an adapter-aware fact source is requested.
- **Inference from SQL.** A column projected as `COALESCE(x, 0)` grounds a nullability annotation through the property's operator rules, not through a fact. Facts are declarations; inference is the propagator's job.

## Sequencing

1. The data model (`Scope`, `WorldRef`, `Fact[K]`, `Channel`, `CompileOrigin`, `ScopeKind`, `FactsByScope`, `FactDiscoverer`, `fact_lookup`, `collect_facts`) and the `Property[K]` additions (`scope_kind`, `facts`, `consistent`). The propagator consults `facts` at every node, runs `consistent` when both inferred and declared are present, and dispatches its walk on `scope_kind`. Ships together with nullability.
2. Nullability discoverers (`not_null` test, column `nullable`, native `NOT NULL`), nullability promoted to a production framework-owned property with `consistent` derived from the precision order. Closes the source-rule piece of [`#26`](https://github.com/dvryaboy/dblect/issues/26).
3. Uniqueness migration (own change; see "Uniqueness migration"). Retires `uniqueness/facts.py` and `uniqueness/propagation.py`, closes [`#16`](https://github.com/dvryaboy/dblect/issues/16).
4. Type discoverer (column `data_type`). First consumer is the semantic-types substrate.
5. Accepted-values and range discoverers. Power the first wave of developer-defined refinements.
6. Config discoverer with concrete per-key fact mappings as detectors adopt them.
7. Compile-value discoverer (`var`, `env_var`, computed) wired to single-value flag assignments. Bridge to the flag world enumerator.

Steps 1 and 2 ship together. The rest are independent and land driven by the consumer.

## Testing

- **Per-discoverer PBT.** Generate manifests and declarations with random metadata; assert each discoverer's facts are a function of its documented input, never invent claims, never drop ones they should produce.
- **Semiring and order laws.** PBT on each property's semiring (associativity, commutativity, identities, distributivity, absorption) and on the derived `consistent` (reflexivity `consistent(k, k)`, and `consistent(declared, default)` for every `K` so an opaque upstream never fails the consistency check). The strict-mode seam diagnostic is a separate layer above `consistent`, exercised by its own test below.
- **Seam diagnostic.** An explicitly opaque `UNKNOWN` meeting a declared refinement is silent. An implicitly erased `UNKNOWN` meeting one is silent at the zero-declaration layer and a finding at the typed layer, and a combine of two committed-but-incompatible operands is a finding at both. The diagnostic names the column, both readings, and the suppression path.
- **Merge-rule PBT.** Associativity and commutativity of each property's `merge`, so reordering discoverers does not change the lookup; and that a contradiction at one scope raises a `BuildIssue` and yields a stable, order-independent provisional value.
- **Tightening and boundary.** For a framework-owned property, an inferred value strictly more precise than the declaration propagates the inferred value as the flow value, keeps the declared value as the boundary, and emits the soft "can tighten" finding. For a user-extended property, the same through a preserving chain, and a clearing step stops the tightening.
- **Asserted-fact end-to-end.** A `not_null` declaration on a column with a `NULLABLE` upstream surfaces a finding and propagates the declared value downstream as provisional. The same declaration with a `NON_NULL` or `UNKNOWN` upstream propagates without a finding. The analogous test for a candidate-key declaration on a derived model.
- **Uniqueness parity.** Before retiring `uniqueness/facts.py`, run both paths against the jaffle fixture and assert agreement on every model's candidate keys.
- **Conditional-fact capture.** A `not_null` or `unique` test with a `where` filter produces a fact with the predicate attached, and `fact_lookup` ignores it.

## Documentation updates on adoption

This is a proposal. Adopting it means evolving `Property[K]` and the propagator, and a few adopted-direction docs state the older shape. Those are left as-is here on purpose: rewriting them now would assert an API still under review, and [`column-level-lineage.md`](./column-level-lineage.md) currently tracks the implemented `property.py`. The substantive rewrites land with the implementation. When adopted:

- [`column-level-lineage.md`](./column-level-lineage.md): `Property[K]` gains `scope_kind`, `facts`, `consistent`, and `depends_on`; `source` folds into `facts`; transfers take a read-only `DepContext`; the propagator evaluates properties in dependency order and dispatches its walk on `scope_kind`, and grows the relation-algebra path.
- [`design-concepts-digest.md`](./design-concepts-digest.md): the "Two lattices, not one" section reconciles to the single-engine framing, with the structural/user-domain split expressed as transfer-rule ownership read off the catalog rather than a stored tag, and the composition-rules line (preserve, erase, drop-to-aggregate, branch-join) reorganises by relational operator into forced-versus-chosen, with the aggregate behaviour named *combinability* and grounded in the measure-additivity and semimodule traditions.
- [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md): the model-keyed `ConditionalUniquenessFact` shape moves to a relation-scoped `Fact[K]` carrying the predicate.

Both docs above carry a forward-pointing note to here in the meantime.

## References

- The substrate this layers on: [`column-level-lineage.md`](./column-level-lineage.md), including the K-relations encoding for uniqueness this migration uses.
- The structural and user-domain transfer vocabulary: [`design-concepts-digest.md`](./design-concepts-digest.md).
- The end-user declaration surface the facts layer carries: [`dblect_technical_intro.md`](./dblect_technical_intro.md) and [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md).
- The current uniqueness facts module: [`../../src/dblect/uniqueness/facts.py`](../../src/dblect/uniqueness/facts.py) and the deferred-activation posture in [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- Foundational literature: Green, Karvounarakis, Tannen (2007) *Provenance Semirings*; Amsterdamer, Deutch, Tannen (2011) *Provenance for Aggregate Queries*; Abiteboul, Hull, Vianu, *Foundations of Databases* (functional-dependency propagation); the type-qualifier tradition (CQual, FlowCaml) for the user-domain lattice; the gradual-typing tradition (Siek and Taha; Wadler and Findler on blame) for the typed/untyped seam.
- Issue [`#26`](https://github.com/dvryaboy/dblect/issues/26): promotes the demo nullability and aggregation-depth properties; the source-rule piece is what this module unblocks. Issue [`#16`](https://github.com/dvryaboy/dblect/issues/16): multi-source uniqueness detectors consume the substrate.
