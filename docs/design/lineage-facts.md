# Lineage facts: grounding annotations from declarations

Status: design
Audience: engineers working on the lineage substrate, on a `Property` that needs values from manifest declarations or developer assertions, or on the flag system that feeds configuration values into propagation. The first half is a tour of how property propagation works, so a reader new to the substrate can start here. It assumes SQL and a passing familiarity with semirings and lattices; it assumes nothing about provenance or type-theory literature, and the references collect the traditions it draws on.

## Motivation

The substrate from [`column-level-lineage.md`](./column-level-lineage.md) gives every property a graph to propagate through. It does not say where values *enter* the graph. Each property has to invent its own grounding, and today the demo properties hard-code constants (`UNKNOWN` for nullability, `0` for aggregation depth) because there is no shared way to read `not_null` tests, declared column types, native constraints, candidate keys, or developer refinement declarations off the manifest.

The capability this unlocks: a developer declares a refinement (say `RevenueNet` on `fct_orders.order_total`, or a candidate key on `dim_customer`) on the model where the meaning lives. The framework propagates the claim downstream as the contract callers can rely on, and checks it against the SQL that produces the model. Without a shared facts module, every property that wants such grounding reimplements manifest plumbing, picks its own precedence rules, and tests its own discovery code.

A `lineage.facts` module makes this a substrate concern. A fact can be about one column or a whole relation, and the substrate treats both uniformly. The uniqueness layer migrates onto it as one property, and the same module is the bridge to the flag system when a config or var carries a refinement.

It is also the convergence point for the three authoring channels the rest of the design relies on: dbt tests and constraints, `meta.dblect.*` blocks in `schema.yml`, and the Python `SemanticType` / `ModelContract` declarations from [`dblect_technical_intro.md`](./dblect_technical_intro.md). The substrate carries all three without exposing any of its machinery to the people writing the declarations.

## How propagation works

A **property** is a value type `K`, a lattice over `K`, and rules for moving a `K` through SQL. `K` is small and ordered by *precision*: a more precise value commits to more about the data. Nullability ranges over `{NON_NULL, NULLABLE, UNKNOWN}`; uniqueness over candidate-key sets `frozenset[frozenset[ColumnRef]]`, where knowing more keys is more precise; a user-domain axis like `contains_tax` over `{TRUE, FALSE, UNKNOWN}`.

The engine is a per-property abstract interpretation over the relational algebra: it computes a sound over-approximation of a property at every node by composing monotone transfer functions, one per SQL operator. Two of the structural properties have extra algebraic structure that the interpretation exploits. Uniqueness key-sets form a bounded distributive lattice (a confluence keeps the keys both branches carry; resolution accumulates keys). Cardinality counts, so its operators are the natural-number semiring (a `JOIN` multiplies, a `UNION ALL` adds). That semiring and the functional-dependency tradition supply the shape of those two properties' transfers. The value-domain properties do not count or accumulate, so they are plain monotone lattice maps. We prove the soundness of each shipped transfer directly rather than inheriting a theorem from any one framework, because the object here is a per-node type annotation rather than a per-tuple provenance value.

The **propagator** walks the lineage graph once per property in dependency order and produces an annotation for every node. At a node with a derivation it reduces the node's expression by recursing into upstream nodes and applying the property's per-operator transfer rules. At a node with no derivation (a source or a seed) it reads the starting value from facts. The walk is single-pass because the property dependency graph is acyclic (below) and the lineage graph is acyclic once window and recursive-CTE regions are treated as opaque boundaries, the same scope cut the rest of the design takes.

Two points carry everything below:

- **One engine, many properties, one pass each.** Adding a property is adding a `Property`, never a new pass. Properties are independent unless one explicitly declares a dependency on another; nullability never consults uniqueness.
- **Annotations degrade, they never lie.** A degraded annotation is `UNKNOWN`-shaped, never a wrong precise value. When sqlglot cannot resolve a column the propagator cannot see what the SQL did, so it emits the lattice top and stays silent: a finding there would be a guess. When a recognized operation clears a *declared* refinement the propagator can name the cause and reports it (the seam and coherence cases under "Validation and propagation").

### Two families of properties

Properties differ by where their transfer rules come from, and that difference is the whole of the structural/user-domain distinction. The engine does not branch on it.

- **Framework transfers** are theorems about SQL semantics, true in every project: a `JOIN` multiplies cardinality, `DISTINCT` introduces a key, `COALESCE(x, 0)` is non-null. Nullability, uniqueness, cardinality, grain, and ordering are built from these. They are the proven core, verified once.
- **User transfers** rest on declared signatures: whether `revenue * 0.9` preserves tax inclusion is what the author meant, which the framework cannot derive. Currency, tax inclusion, gross/net, and the other user-domain axes are built from these, in an open catalog users extend.

A finding carries the assumptions in its derivation, which the propagator traces: one built only from core transfers is unconditional, one that passes through a user signature holds given that signature. Soundness obligations differ in kind, not in machinery (see "Soundness contract"): the framework proves its transfers are monotone and sound; a user transfer is correct *given* that the author's declared signature is monotone and means what they say.

### Transfer rules by operator

A property's behaviour is indexed by relational operator. Most of it is forced by the lattice rather than chosen.

- **Filter / selection**: preserve. Forced.
- **Confluence (`UNION ALL`)**: the lattice join of the branches. For nullability, nullable if either branch is; for uniqueness, a key survives only if both branches carry it. Forced. `UNION` (distinct) is the same join with one operator-specific addition: set semantics dedups, so the whole projected row becomes a candidate key. The confluence rule is therefore keyed on the specific operator, not on a single fixed join, because `UNION` and `UNION ALL` have different row semantics.
- **Cross (`JOIN`)**: each side preserves; a multi-input scalar expression folds its operands by the operator's rule. Forced for the structural core.
- **Scalar / projection**: preserve, transform, or clear. A genuine choice. An identity (`Alias`, a bare `Column`) preserves and is where tightening happens; a declared map (a currency conversion, a `discount` or `tax` annotation) transforms; an opaque scalar or bare literal clears the value to the lattice top. A binary combine (`a + b`) preserves when operands agree on the axis, raises a finding when two committed operands are incompatible (tax-inclusive plus tax-exclusive), and clears to top when a committed operand meets an unrefined one, recording why it cleared so the seam rule can decide whether to speak.
- **Aggregation**: the aggregate transfer, whose behaviour is the measure's *combinability*. A genuine choice.

So a property chooses behaviour only at scalar transforms and at aggregation; the rest follows from the lattice.

The aggregate transfer asks whether a measure's meaning survives a `GROUP BY`, and under what precondition. Three outcomes cover it: **preserved** (a value-returning aggregate over a normal measure keeps its axes), **preserved under coherence** (it survives only where named columns are constant in the aggregation scope), or **cleared** (no aggregate preserves it, as for a ratio). An aggregate transfer must commute with confluence and cross, so that the single-pass walk gives the same annotation regardless of whether an aggregate sits above or below a `UNION ALL`; the framework discharges this for shipped aggregates and requires it of user-supplied ones. Aggregation is the one place the bare lattice does not force the rule, which is why it gets its own slot. The user-land vocabulary that compiles to these outcomes lives in the types layer; the v1 surface is a coherence declaration (`within=<cols>`) plus a flag for measures that never aggregate.

### Properties reading one another, in dependency order

Most properties propagate alone. A few need another property's annotations to compute their own transfers, and two cases carry the design.

*Cardinality reads uniqueness.* To tell a fan-out join from a key-preserving one, the cardinality transfer at a `JOIN` asks whether the join key is unique on the other side. That answer is the uniqueness property's annotation, read at the join node. Both come from the proven core.

*A user-defined money type reads a functional dependency.* Currency is not built in; it is a refinement a developer declares (a `Money` semantic type carrying a currency axis), which the types layer compiles to a property like any other. Take `SELECT region, SUM(amount) AS total FROM orders GROUP BY region`, where `amount` is typed `Money`. Does `total` keep its currency? Only if every row folded into a group already shares one, that is, only if `region -> currency` holds. The compiled transfer reads that functional dependency. Where it holds, currency is preserved; otherwise the framework cannot guarantee a group shares one currency, so the transfer clears the axis and the audit flags it. The sum may be mixing currencies, which is the bug `within="currency"` exists to catch. It does not widen to top in silence, because losing a declared refinement is cause for investigation.

A property names the properties its transfers read in `depends_on`, and the propagator evaluates those first. A transfer reaches a dependency only through a read-only `DepContext` typed to the declared dependencies, never a shared global map. The edge is a wire: it sets evaluation order and is the sole channel for the read. A transfer that did not declare an edge cannot type a read of that annotation, so a missing or mistyped edge is a type error at authoring time rather than stale state at runtime.

The channel does not manufacture information. If no one ever typed `amount` as `Money`, there is no currency refinement on that column, and the mixed-currency `SUM` draws no finding. That is the substrate's posture (absence is silence), not a gap the channel could close.

The `depends_on` graph must be acyclic, so no pair of properties needs a joint fixpoint over a product lattice. An authoring lint warns when a core transfer reads a user-declared axis, since that quietly makes a structural conclusion conditional. The user writes none of this: the edge originates in the coherence declaration on the `Money` type, and the framework compiles that intent into the dependency and the transfer.

## What a fact is

A **fact** is a typed claim about one node of the lineage graph, under one property, with provenance. A node is either a column or a relation, and those are the only two subjects a fact can have. Anything that looks multi-column is a relation fact whose *value* names the columns: a candidate key `{customer_id, region}` is the statement "this relation is unique on `{customer_id, region}`," so it attaches to the relation and the column set lives in the value, never in the address.

A fact grounds a node in one of two ways, depending on whether the node has a derivation:

- **Anchoring.** No derivation (a source or seed column, or the source relation itself). The fact is the only input the propagator has.
- **Asserted.** The node is derived (a model output column, a model's candidate key emerging from a `SELECT`). The fact is a developer or contract claim about what the derivation produces. The propagator uses it forward and checks it against the upstream.

Facts must be rock-solid because detectors rely on them silently. A wrong fact produces a wrong annotation produces a false-positive finding; an absent fact produces a missing annotation produces a silent skip. The audit is louder when it knows and quieter when it does not.

## Data model

The data model makes illegal states unrepresentable rather than legal-by-docstring. A fact is parameterized by both its value type and its scope kind, so a column property cannot be handed a relation fact. Provenance is a sealed union, so a field that is meaningful only for one kind of fact exists only on that kind.

```python
from typing import Any, Callable, Collection, Generic, Mapping, Protocol, TypeVar
from dataclasses import dataclass
from enum import StrEnum

from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.expr import Expr            # the sqlglot expression wrapper
import sqlglot.expressions as exp

K  = TypeVar("K")
K2 = TypeVar("K2")
S  = TypeVar("S",  ColumnRef, SourceRef)   # a property is column- OR relation-scoped, never both
S2 = TypeVar("S2", ColumnRef, SourceRef)

# A world assignment chosen by the flag layer: the value each enumerated flag takes
# in this run. Opaque to the substrate; defined by the flag system.
WorldRef = Mapping[str, object]


class ScopeKind(StrEnum):
    COLUMN   = "column"    # propagator walks per-column projections
    RELATION = "relation"  # propagator walks relation-algebra structure
```

### Provenance

Provenance records where a fact was authored, for tracing an annotation back to its grounding and for reporting. It carries no authority ordering: conflicts are resolved by the lattice, never by ranking channels. Each variant carries exactly the fields valid for it.

```python
class DeclaredSource(StrEnum):
    DBT_GENERIC_TEST = "dbt_generic_test"  # not_null, unique, accepted_values, …
    DBT_UTILS_TEST   = "dbt_utils_test"    # unique_combination_of_columns, accepted_range, …
    COLUMN_METADATA  = "column_metadata"   # data_type, nullable in schema.yml
    DBT_META         = "dbt_meta"          # meta.dblect.* blocks in schema.yml
    MODEL_CONTRACT   = "model_contract"    # dbt model-contract declaration
    USER_ASSERTED    = "user_asserted"     # Python SemanticType / Field / ModelContract


class CompileOrigin(StrEnum):
    DBT_VAR    = "dbt_var"     # var() from dbt_project.yml; statically enumerable
    ENV_VAR    = "env_var"     # env_var(); statically enumerable
    DBT_CONFIG = "dbt_config"  # node.config[...] key
    COMPUTED   = "computed"    # Jinja/Python substitution, possibly a DB call; opaque to enumeration


@dataclass(frozen=True, slots=True)
class Declared:
    """Authored directly: a dbt test, schema.yml metadata or meta, or a Python contract."""
    source: DeclaredSource


@dataclass(frozen=True, slots=True)
class NativeConstraint:
    """A warehouse or dbt 1.5+ constraint. ``enforced_on_write`` exists only here and
    records whether the active adapter enforces the constraint on write. It is read by
    the unenforced-constraint finding, never by fact resolution."""
    enforced_on_write: bool


@dataclass(frozen=True, slots=True)
class CompileValue:
    """A value resolved at compile time. ``world`` exists only here and is never absent:
    a compile value is ground truth in exactly the world the flag layer fixed for this
    run. ``origin`` decides whether the flag layer can enumerate worlds over it."""
    origin: CompileOrigin
    world:  WorldRef


Provenance = Declared | NativeConstraint | CompileValue


@dataclass(frozen=True, slots=True)
class Fact(Generic[K, S]):
    """One claim about one node (a column when S is ColumnRef, a relation when S is
    SourceRef), under one property."""
    scope:      S
    value:      K
    provenance: Provenance
    detail:     str | None = None
```

### The flowing annotation

The propagator stores and passes an `Annotation`, not a bare `K`. Two bits ride alongside the value because diagnostics depend on them, and a bare lattice value has nowhere to put them.

```python
class Opacity(StrEnum):
    REFINED  = "refined"   # the value carries information
    EXPLICIT = "explicit"  # value is top because the modeler declared the node opaque; flows silently
    IMPLICIT = "implicit"  # value is top because nothing annotated it; warns where it meets a refinement


@dataclass(frozen=True, slots=True)
class Annotation(Generic[K]):
    value:       K
    opacity:     Opacity = Opacity.REFINED
    provisional: bool = False   # computed downstream of a contract the SQL does not currently honor
```

`provisional` is a taint with a defined rule: it is set when a node's inferred value conflicts with its declared value, it is the logical OR of its transfer inputs, and it clears when a node is freshly anchored by a fact the inferred value is consistent with. Detectors may downgrade findings that rest on a provisional annotation.

### Grounding a node from facts

Grounding a node returns one of three results, so "no fact" and "declared opaque" are distinct rather than both folded into a top-valued lookup. This distinction is load-bearing for the seam diagnostic.

```python
@dataclass(frozen=True, slots=True)
class Anchor(Generic[K]):
    value: K                 # seed/assert this value; flows as Annotation(value, REFINED)


@dataclass(frozen=True, slots=True)
class Opaque:
    """Explicit opt-out declared on the node. Flows as Annotation(top, EXPLICIT), silently."""


@dataclass(frozen=True, slots=True)
class Absent:
    """No fact. The propagator walks; an unknown that results is IMPLICIT."""


Grounding = Anchor[K] | Opaque | Absent     # the K-parameterized result of grounding one node
```

### The lattice: one source for order, resolution, and consistency

A property states its order once, as a `Lattice`. Fact resolution and the validation check both derive from it, so they cannot drift apart.

```python
@dataclass(frozen=True, slots=True)
class Lattice(Generic[K]):
    """``meet`` is the greatest lower bound (the more precise value), ``join`` the least
    upper bound (used at a confluence). ``top`` is 'no information'; ``bottom`` is
    'contradiction', a value that no data can satisfy."""
    meet:   Callable[[K, K], K]
    join:   Callable[[K, K], K]
    top:    K
    bottom: K

    def refines(self, finer: K, coarser: K) -> bool:
        return self.meet(finer, coarser) == finer


def resolve(lat: Lattice[K], facts: tuple[Fact[K, Any], ...]) -> tuple[K, bool]:
    """Fold every fact at one scope to the most precise value consistent with all of
    them. Meet is associative and commutative by the lattice laws, so the result does
    not depend on discoverer order. A result of ``bottom`` means the declarations are
    mutually unsatisfiable; the caller raises a BuildIssue and keeps this deterministic
    value so the run stays reproducible."""
    value = lat.top
    for f in facts:
        value = lat.meet(value, f.value)
    return value, value == lat.bottom


def consistent(lat: Lattice[K]) -> Callable[[K, K], bool]:
    """The inferred value honours the declaration when the SQL revealed nothing (top) or
    proved something at least as precise. Derived from ``refines``, never hand-written."""
    def check(declared: K, inferred: K) -> bool:
        return inferred == lat.top or lat.refines(inferred, declared)
    return check
```

The three property shapes instantiate the one lattice:

| Property            | `top`       | `x` refines `y` when …            | `meet` (resolve)        | `join` (confluence)              | `bottom` reachable |
|---------------------|-------------|-----------------------------------|-------------------------|----------------------------------|--------------------|
| Nullability         | `UNKNOWN`   | `x` is a stronger non-null guarantee | the stronger guarantee | weaker (either-null is nullable) | no                 |
| Uniqueness          | `{}` (no keys) | `x` knows a superset of `y`'s keys | union of keys         | keys both branches carry         | no                 |
| User-domain axis    | `UNKNOWN`   | `x == y`, or `y` is `UNKNOWN`     | equal value, else `bottom` | equal value, else `UNKNOWN`   | yes                |

A genuine contradiction is `meet == bottom`. It is reachable only for the equality-shaped user-domain axes, where two declarations naming different values (a contract says `contains_tax=TRUE`, a meta block says `FALSE`) cannot both hold. Structural properties never contradict: a `not_null` constraint and a permissive `nullable: true` declaration meet to the stronger guarantee, and two candidate-key declarations simply union.

### Properties, transfers, and discoverers

```python
@dataclass(frozen=True, slots=True)
class PropertyRef(Generic[K2, S2]):
    """A typed handle to a property. The K2 and S2 are recovered at a read site, so a
    transfer reading a dependency gets that dependency's value and scope types back,
    not ``object``. Equality is on ``name``."""
    name: str


class DepContext(Protocol):
    def annotation(self, ref: PropertyRef[K2, S2], scope: S2) -> Annotation[K2] | None: ...


# Transfers receive and return annotations, so opacity and the provisional taint flow
# through them. A property with no dependencies ignores the DepContext.
OperatorTransfer  = Callable[[Expr, tuple[Annotation[K], ...], DepContext], Annotation[K]]
AggregateTransfer = Callable[[exp.AggFunc, Annotation[K], DepContext], Annotation[K]]


class FactDiscoverer(Protocol[K, S]):
    """Reads the manifest and dblect declarations, returns facts for any node it can
    ground. Pure, and it returns a materialized collection so that a discoverer which
    raises drops all of its facts and none of another's."""

    def discover(
        self, manifest: "Manifest", *, name_to_source: Mapping[str, SourceRef],
    ) -> Collection[Fact[K, S]]: ...


@dataclass(frozen=True, slots=True)
class Property(Generic[K, S]):
    ref:        PropertyRef[K, S]                 # the property's own typed handle; name lives here
    scope_kind: ScopeKind                         # runtime walk dispatch; pinned by S at the type level
    lattice:    Lattice[K]                        # the single algebraic source
    operators:  Mapping[type[Expr], OperatorTransfer[K]]
    aggregates: Mapping[type[exp.AggFunc], AggregateTransfer[K]]
    ground:     Callable[[S], Grounding[K]]       # Anchor | Opaque | Absent for a node
    depends_on: tuple[PropertyRef[Any, Any], ...] = ()
```

`consistent` and `resolve` are derived from `lattice`, so they are not fields. A constructor wires a property from its discoverers:

```python
def nullability_property(
    manifest: "Manifest", *, name_to_source: Mapping[str, SourceRef],
    extra: tuple[FactDiscoverer[Nullability, ColumnRef], ...] = (),
) -> Property[Nullability, ColumnRef]:
    facts = collect(manifest, (*_NULLABILITY_DISCOVERERS, *extra), name_to_source=name_to_source)
    return Property(
        ref=PropertyRef("nullability"),
        scope_kind=ScopeKind.COLUMN,
        lattice=NULLABILITY_LATTICE,
        operators=_NULLABILITY_OPERATORS,
        aggregates=_NULLABILITY_AGGREGATES,
        ground=grounding(facts, NULLABILITY_LATTICE),
    )
```

`grounding` turns the collected facts into the per-node lookup: it folds a scope's bucket through `resolve`, raises on a `bottom` contradiction, returns `Opaque` where the node carries an explicit opt-out declaration, `Anchor` where a value resolved, and `Absent` otherwise.

## Resolving multiple facts at a scope

Several discoverers can ground the same node. `resolve` folds the bucket with the lattice meet, which is the most precise value consistent with every claim. Because meet is associative and commutative, the result is independent of discoverer registration and dict iteration order. A `bottom` result is a genuine contradiction: the build surfaces a `BuildIssue`, keeps the deterministic `bottom`-derived value so the run reproduces, and marks downstream annotations provisional. Provenance stays on each fact for tracing and reporting, and never enters resolution.

**Compile-value facts share one world.** The flag layer fixes one world per propagation run, and the compile-value discoverers emit their facts under it, so every fact in a bucket shares that world and resolution is ordinary. A var-derived value is ground truth in its world, the same standing as a native constraint or a user assertion. A difference *between* worlds is the flag-world analysis ([`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md)), reported as "this contract holds under world A and fails under world B." Where a world-scoped value and an unconditional assertion genuinely disagree on one axis (a contract claims `contains_tax=TRUE` always, world B produces `FALSE`), that disagreement is the finding the analysis exists to raise.

A single propagation under a single world is one evaluation of each property's derivation. The same derivation evaluated under different world assignments is what the flag-world analysis compares, so multi-world cost is driven by re-evaluating derivations under assignments rather than by anything in this module. The audit exposes a "trace this annotation to its grounding facts" helper that reconstructs a derivation on demand.

## Discovery

A discoverer per axis. The substrate ships discoverers for the axes production properties need first; user properties register their own. A discoverer is pure and total within its axis: every node it claims authority over either gets a fact or is silently skipped. It never emits a top-valued fact pretending to be a claim; an explicit opaque opt-out is its own declaration, surfaced as `Opaque` grounding rather than as a value.

| Axis                | Manifest / declaration input                                   | Scope    |
|---------------------|----------------------------------------------------------------|----------|
| Nullability         | `not_null` test, column `nullable` flag, native `NOT NULL`     | column   |
| Type                | column `data_type`                                             | column   |
| Accepted-values     | `accepted_values` test, native `CHECK ... IN (...)`            | column   |
| Range               | `dbt_utils.accepted_range`, native `CHECK x BETWEEN ...`       | column   |
| Tags / meta         | column-level `tags` and `meta` keys                            | column   |
| Candidate key       | `unique` test, `unique_combination_of_columns`, native `PRIMARY KEY` / `UNIQUE` | relation |
| Row-count interval  | `dbt_utils.expression_is_true` shaped as a count assertion     | relation |

Two discoverers are forward-looking; their plumbing lands with this module even though their per-key mappings arrive with the consumers. Both emit `CompileValue` facts scoped to the world the flag layer chose:

- **Config-derived facts.** Reads `node.config` keys a property cares about (`materialized`, `incremental_strategy`) and produces relation facts (`origin=DBT_CONFIG`).
- **Compile-resolved values.** Produces facts where a refinement type's `affects` clause has a single value under the chosen world. The value need not come from a dbt `var()`: an `env_var()`, or Jinja or Python that computes a value at compile time (including a macro that runs a warehouse query), reaches the manifest the same way. Where the value is statically enumerable (`origin=DBT_VAR`, `ENV_VAR`) the flag layer enumerates worlds over it; where it is computed opaquely (`origin=COMPUTED`) the flag layer sees the single resolved value as one world, matching [`var-inference-spec.md`](./var-inference-spec.md).

### From declaration to fact

All three authoring channels reduce to a `Fact`, and a developer writing a declaration never meets `Lattice`, `Grounding`, or the transfer catalogs.

| What the developer writes | Channel | Becomes |
|---|---|---|
| `not_null` / `unique` test, native constraint, column `data_type` | dbt manifest | structural grounding fact (`Declared(DBT_GENERIC_TEST)`, `NativeConstraint`, `Declared(COLUMN_METADATA)`) |
| `meta.dblect.*` in `schema.yml` | manifest meta (read-only in v1) | bridge fact (`Declared(DBT_META)`) |
| `order_total: RevenueNet = Field(non_negative=True)` on a `ModelContract` | Python declaration registry | user-domain fact (`Declared(USER_ASSERTED)`) |
| `SemanticFlag.affects` resolved under a world | flag world enumerator | `CompileValue` fact scoped to that world |

A worked example, the user-domain channel. A developer writes the Pandera-shaped declaration from the intro doc:

```python
class FctOrders(ModelContract):
    dbt_model = "marts.fct_orders"
    order_total: RevenueNet = Field(non_negative=True)
```

A discoverer reading the declaration registry returns:

```python
Fact(
    scope=ColumnRef(SourceRef("model.shop.fct_orders"), "order_total"),
    value=RevenueNet,                       # the refinement the developer declared
    provenance=Declared(DeclaredSource.USER_ASSERTED),
)
```

Nothing in that path requires the author to know a fact store exists. The structural channels work the same way against `not_null` tests and native constraints, and the flag channel against `affects` under a chosen world. This is the round-trip check that the substrate carries the end-user surface: the declaration produces facts, the facts feed propagation, and propagation produces the boundary checks and findings the developer sees.

## Validation and propagation

At a node the propagator has up to two inputs: the **inferred** annotation, from walking the upstream expression (absent at sources and seeds), and the **declared** value, from `ground` (absent when no fact applies). Two independent decisions follow.

**Validation** runs `consistent(declared, inferred.value)`. It holds when the SQL revealed nothing (`inferred` is top) or proved something at least as precise as the declaration. A property never overrides this; it is derived from the property's lattice, so it cannot drift from the order that resolution uses.

**Propagation** decides what flows onward. The node carries one propagating annotation, the **flow** value: the most precise value the framework can justify, where "justify" means every step from the declared inputs to here preserved or combined the value (a theorem or a user signature) rather than clearing it. A declared node additionally pins a **boundary** value, the value it publishes to other models. The boundary is not a second propagating lattice; it is the contract a model exposes at its edge. Within the model, downstream nodes read the flow value. When a downstream model references this column, it anchors on the boundary value if one was declared, otherwise on the flow value, so a consumer that built against a deliberately loose contract is insulated from internal tightening.

| inferred                        | declared | consistent     | flow (within model)   | boundary (exported) | finding |
|---------------------------------|----------|----------------|-----------------------|---------------------|---------|
| absent                          | absent   | —              | top (`IMPLICIT`)      | none                | none |
| present                         | absent   | —              | inferred              | inferred            | none |
| absent                          | present  | —              | declared              | declared            | none (anchors a source) |
| top, `EXPLICIT` opt-out         | present  | yes (vacuous)  | declared              | declared            | none (opacity declared) |
| top, `IMPLICIT`                 | present  | yes (vacuous)  | declared              | declared            | typed layer: "guarantee unverified" (seam rule) |
| refines declared                | present  | yes            | inferred              | declared            | soft "can tighten" if strictly more precise |
| conflicts                       | present  | no             | declared (provisional)| declared            | hard finding |

The two rows that carry the design:

- **`refines declared` (tightening).** The SQL proves something at least as precise as the declaration, so the flow value is the inferred one. For a structural property this is unconditional (a `COALESCE` makes the column non-null whatever the declaration said); for a user-domain property it tightens only through preserving steps, because those are the justified ones. The boundary stays at the declared value, so external consumers are unaffected and a developer keeps the right to publish a deliberately loose contract. When the inferred value is strictly more precise, the audit emits a suppressible "you can tighten this, or confirm the looseness is intentional," softer for user-domain axes where deliberate abstraction is common.
- **`conflicts` (violation).** The inferred value contradicts the contract. The audit raises a finding at the violation site, propagation continues from the declared value, and downstream annotations are marked `provisional`. This is error recovery: once the error is reported, assume the declared value so one upstream regression does not blank analysis of every consumer.

### Erasure at the typed/untyped seam

A refined value meeting an unrefined one is where the highest-value bugs hide and where a partial adopter most wants a nudge. dblect follows the gradual-typing tradition here (see references): separate an explicit opt-out from an absent annotation, and treat them oppositely. The `Opacity` tag on the flowing annotation is exactly this distinction, carried through transfers so the binary combine can decide whether to speak:

```python
def combine(lat: Lattice[K], a: Annotation[K], b: Annotation[K]) -> Annotation[K]:
    m = lat.meet(a.value, b.value)
    if m == lat.bottom:
        raise SeamContradiction(a, b)                       # two committed, incompatible operands
    if a.value == b.value == m:
        return Annotation(m, provisional=a.provisional or b.provisional)   # agree: preserve
    cleared = a if a.value == lat.top else b                # one committed, the other top: clears
    return Annotation(lat.top, opacity=cleared.opacity,
                      provisional=a.provisional or b.provisional)
```

- A top value the modeler *declared* (an `OpaqueEffect`, a column marked opaque) is `EXPLICIT`. It flows silently, because the modeler took responsibility.
- A top value that is merely *un-annotated* is `IMPLICIT`. Where it clears a declared refinement, the audit speaks up. The diagnostic is on once a project has declared semantic types and off at the zero-declaration layer, so the signal lands where the investment already is.

The same rule covers any clearing of a declared refinement: an aggregate whose coherence precondition is not met (the mixed-currency `SUM`) and an opaque scalar that drops an axis both lose a declared refinement, so both report. Only an undeclared value (`IMPLICIT` is one form, absence the other) under no declaration, or an explicit opt-out, clears without a word.

The runtime layer is the check at the seam: the static side notes the boundary, and the generator probes whether the unrefined side actually respects the refined side's assumption.

The diagnostic is a fixed template, not synthesized prose. dblect cannot author domain narrative like "this mixes tax-inclusive and tax-exclusive amounts," because it does not know what a user-domain axis means. It fills slots from what it has: the site, the operator, the two operand columns and their types, the axis that cleared, and the suppression path. The only domain-flavored text is a name the modeler chose, the type's own name and an optional one-line description from the declaration, with fallback to the bare type and axis names. A realistic rendering:

> `orders.sql:12`: `total` combines `revenue` and `net_revenue` with `+`. `net_revenue` is `RevenueWithTax` but `revenue` carries no refinement on `contains_tax`, so the result drops it. Annotate `revenue` as `RevenueWithTax` if it qualifies, or treat this as a possible mismatch. To silence: mark `revenue` opaque, or disable `refinement-erased-at-seam` for this model.

## Soundness contract

1. **Discoverer correctness is a hard guarantee for the input it reads.** A discoverer that emits a fact its declaration does not support is a substrate bug. PBT covers each shipping discoverer. Whether the resulting conclusion is unconditional depends on what it rests on: one built only from core transfers is a theorem given the declared inputs; one that uses a user signature holds given the declared inputs and that signature.
2. **Transfers are monotone, and that obligation is explicit.** A framework transfer is proven monotone and a sound over-approximation, once. A user-supplied transfer must be monotone with respect to the property's order; this is the obligation the author's vouch discharges, and the runtime layer is where an inaccurate vouch is caught empirically. An aggregate transfer additionally commutes with confluence and cross.
3. **Absence is silence.** A node the input does not cover grounds as `Absent`, the propagator returns the lattice top, and detectors read it as "we don't know."
4. **Conditional facts are captured but not activated.** A `not_null` or `unique` with a `where` filter produces a fact carrying the predicate, and grounding ignores the predicate for now. A `where` filter is selection, which the provenance tradition handles by conditioning the annotation; activation follows the rule [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md) commits to, so the deferral is engineering sequencing rather than an open question.
5. **Contradictions are resolved and surfaced.** Two declarations whose values meet to `bottom` raise a `BuildIssue`; resolution keeps a deterministic value and never picks a winner from provenance.
6. **Facts cross model boundaries only through propagation.** The flow value carries downstream through the lineage graph; the boundary value gates cross-model contract checks.
7. **Asserted facts are checked, and the boundary is stable.** A fact on a derived node runs through `consistent` against the inferred value. A mismatch is a finding. The declared value remains the contract callers built against, and downstream-of-violation annotations are provisional.

## Trusting unenforced constraints

Framework transfers are theorems; leaf facts are not. The transfer rules are proven once, but the candidate key, foreign key, or `not_null` claim that seeds the propagation is an assertion the framework cannot verify, since it never reads source data. So even a core conclusion is conditional: given the declared source facts, the propagated values are theorems. "Proven" means proven from the declared inputs, not verified against data. A constraint the warehouse declares but does not enforce is a leaf-fact risk, not a transfer-rule risk: it can make a propagated annotation wrong about the data while the rules that produced it stay sound.

Many warehouses (Snowflake, BigQuery, Redshift, Databricks) treat `PRIMARY KEY`, `UNIQUE`, and `FOREIGN KEY` as informational. Some support a `RELY` form the optimiser trusts for rewrites without validating the data, the same conditional bet this substrate makes; others are documentation only. Whether a native constraint actually backs its claim is an adapter-and-constraint-kind question (Databricks enforces `CHECK` but not `PRIMARY KEY`, Snowflake enforces neither), captured by `enforced_on_write` on the `NativeConstraint` provenance. For dblect's purposes the question collapses to one: is the claim checked against data by something that runs? A dbt `unique` test is, because the runtime layer runs it; an advisory `PRIMARY KEY` is not. This is why provenance carries no authority order: the signal that matters is whether a running guard exists, read where it is needed.

Two things follow:

- **Discoverers are adapter-aware about enforcement.** The native-constraint discoverer knows the active adapter and sets `enforced_on_write` on each fact. This is descriptive provenance; resolution never reads it.
- **The runtime layer is the backstop, and the gap gets a finding.** The audit's empirical checks and the generator intents named for these violations (Orphan, NullKey, Duplicate, Boundary) test whether advisory constraints hold. Where a load-bearing structural annotation rests on a native constraint with `enforced_on_write=False` that no running test covers at the same scope, the audit emits a finding ("uniqueness on `dim_customer.id` rests on an advisory `PRIMARY KEY` and no `unique` test guards it; add a test"), turning a silent assumption into a recommendation.

## Coverage and degradation

Silent degradation is sound but it can hide behind itself: a manifest where sqlglot resolves few columns produces few annotations and few findings, which can read as a clean bill rather than as thin coverage. The audit therefore treats resolution coverage as a first-class output, not a footnote. Per model it reports the fraction of columns the propagator resolved against the fraction it fell blind on, and per discoverer how many nodes it grounded. A configurable floor turns sustained blindness into a finding ("resolved 38% of columns on `fct_orders`; the contract checks below cover only what was resolved"), so a reviewer sees thin coverage as thin coverage. The default posture stays silent-on-blindness for individual nodes; the floor is about the aggregate.

## Position relative to existing substrate

```
   audit detectors
          ↓
   Property + propagate(graph, prop)
          ↓
   lineage.facts          ←  uniqueness migrates onto this (see "Uniqueness migration")
          ↓
   Manifest + dblect declarations (Node, Column, DbtTestMetadata, ConstraintSpec, SemanticType, …)
```

## Uniqueness migration

Uniqueness is the worked example for relation-scoped facts.

**Encoding.** Uniqueness becomes a `Property[CandidateKeySet, SourceRef]`, built entirely from core transfers. The K-relations encoding from [`column-level-lineage.md`](./column-level-lineage.md) (`K = frozenset[frozenset[ColumnRef]]`) supplies the algebra. The candidate key is the *value* at the relation node, never a column-set address. Confluence keeps the keys both branches carry (and `UNION` adds the whole projected row as a key); a `JOIN` combines keys subject to join-condition coverage; `DISTINCT` and top-level `GROUP BY` introduce the projection set as a key.

**Discoverers.** All produce relation facts whose value is a key set:

| Manifest input                                 | Fact                                                            |
|------------------------------------------------|-----------------------------------------------------------------|
| `unique` test on column `c`                    | `Fact(model, {{c}}, Declared(DBT_GENERIC_TEST))`                |
| `unique_combination_of_columns(c1, c2, …)`     | `Fact(model, {{c1, c2, …}}, Declared(DBT_UTILS_TEST))`          |
| Native `PRIMARY KEY (c1, c2)`                  | `Fact(model, {{c1, c2}}, NativeConstraint(enforced_on_write=…))`|
| Native column-level `UNIQUE` on `c`            | `Fact(model, {{c}}, NativeConstraint(enforced_on_write=…))`     |

Resolution is the lattice meet, which for uniqueness unions independent declared keys, so no contradiction arises. Provenance records which declaration each key came from, for the trace.

**What requires care.** The relation-algebra walk is new substrate. The K-relations literature is most natural at the row level, and lifting to per-node annotations means a transfer rule has to be explicit about whether it reads the upstream relation's annotation or the upstream columns' annotations. The operator rules in `column-level-lineage.md` get this right for uniqueness; new relation-scoped properties should reuse the pattern. Conditional uniqueness facts carry over with the same deferral as [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md): the substrate captures the predicate, activation lands when a concrete consumer asks.

**Sequencing.** The migration is its own change after the substrate lands with nullability. The existing uniqueness path keeps backing the detectors while the new path is built, a "both paths agree on jaffle" test pins parity for the cut-over, and after cut-over the old path retires. This closes [`#16`](https://github.com/dvryaboy/dblect/issues/16): facts on a `JOIN`'s upstream propagate through the cross rule to the output, so the multi-source special case stops being special.

## What this does not cover

- **Activation of conditional facts.** See [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- **World enumeration over flag values.** Belongs to the flag system. This module supplies values inside a world; the flag layer chooses worlds and compares evaluations across them.
- **Cross-package fact inference.** Facts declared in a dbt package and consumed by a downstream package that does not import it. Same scope cut as [`var-inference-spec.md`](./var-inference-spec.md).
- **Runtime facts from the warehouse.** `INFORMATION_SCHEMA` or adapter-side metadata. Lands when an adapter-aware fact source is requested.
- **Inference from SQL.** A column projected as `COALESCE(x, 0)` grounds a nullability annotation through the property's operator rules, not through a fact. Facts are declarations; inference is the propagator's job.
- **Window and recursive-CTE propagation.** Treated as opaque boundaries that re-anchor on output, the same scope cut the rest of the design takes; this keeps the per-property walk single-pass.

## Sequencing

1. The data model (`Fact`, `Provenance`, `Annotation`, `Grounding`, `Lattice`, `PropertyRef`, `DepContext`, `FactDiscoverer`, `collect`, `grounding`) and the `Property` shape (`scope_kind`, `lattice`, `ground`, `depends_on`). The propagator grounds at every node, runs `consistent` when both inferred and declared are present, carries `Annotation` through transfers, and dispatches its walk on `scope_kind`. Ships with nullability.
2. Nullability discoverers (`not_null` test, column `nullable`, native `NOT NULL`), nullability promoted to a production property. Closes the source-rule piece of [`#26`](https://github.com/dvryaboy/dblect/issues/26).
3. Uniqueness migration (own change). Closes [`#16`](https://github.com/dvryaboy/dblect/issues/16).
4. Type discoverer (column `data_type`). First consumer is the semantic-types substrate.
5. Accepted-values and range discoverers. Power the first wave of developer-defined refinements.
6. Config discoverer with concrete per-key fact mappings as detectors adopt them.
7. Compile-value discoverer (`var`, `env_var`, computed) wired to single-value flag assignments. Bridge to the flag world enumerator.

Steps 1 and 2 ship together. The rest are independent and land driven by the consumer.

## Testing

- **Per-discoverer PBT.** Generate manifests and declarations with random metadata; assert each discoverer's facts are a function of its documented input, never invent claims, never drop ones they should produce, and never emit a top-valued claim.
- **Lattice laws.** PBT on each property's lattice (associativity, commutativity, idempotence of meet and join, absorption, the `top`/`bottom` identities) and on the derived `consistent` (reflexivity, and `consistent(declared, top)` for every value so an opaque upstream never fails the check). Because resolution and `consistent` are derived from the lattice, this is the single place those laws are tested.
- **Transfer obligations.** Monotonicity of each shipped transfer; commutation of each aggregate transfer with confluence and cross. A user-domain property's transfers are tested for monotonicity in the same harness so a registered axis cannot break the property quietly.
- **Seam diagnostic.** An `EXPLICIT` top meeting a declared refinement is silent; an `IMPLICIT` top meeting one is silent at the zero-declaration layer and a finding at the typed layer; two committed incompatible operands are a finding at both. The diagnostic names the column, both readings, and the suppression path.
- **Resolution determinism.** A bucket of facts in any order resolves to the same value; a `bottom` contradiction raises a `BuildIssue` and yields the same deterministic value regardless of order.
- **Tightening and boundary.** A structural property whose inferred value is strictly more precise than the declaration propagates the inferred value as flow, keeps the declared value as boundary, and emits the soft finding. A user-domain property does the same through a preserving chain, and a clearing step stops the tightening.
- **Asserted-fact end-to-end.** A `not_null` declaration on a column with a `NULLABLE` upstream surfaces a finding and propagates the declared value downstream as provisional; the same with a `NON_NULL` or top upstream propagates without a finding. The analogous test for a candidate-key declaration on a derived model.
- **Coverage reporting.** A deliberately under-resolvable model reports low coverage and trips the floor finding.
- **Uniqueness parity.** Before retiring the old uniqueness path, run both against the jaffle fixture and assert agreement on every model's candidate keys.

## Companion docs to update on adoption

Adopting this evolves `Property` and the propagator, so a few companion docs gain the new shape when the implementation lands:

- [`column-level-lineage.md`](./column-level-lineage.md): `Property` gains `scope_kind`, `lattice`, `ground`, and `depends_on`; transfers take a read-only `DepContext` and carry `Annotation`; the propagator evaluates properties in dependency order, dispatches its walk on `scope_kind`, and grows the relation-algebra path.
- [`design-concepts-digest.md`](./design-concepts-digest.md): the structural/user-domain split is expressed as where a property's transfers come from (the proven core or a user declaration), with the composition rules organised by relational operator into forced-versus-chosen and the aggregate behaviour named *combinability*.
- [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md): the model-keyed conditional fact becomes a relation-scoped `Fact` carrying the predicate.

## References

- The substrate this layers on: [`column-level-lineage.md`](./column-level-lineage.md), including the K-relations encoding for uniqueness.
- The structural and user-domain transfer vocabulary: [`design-concepts-digest.md`](./design-concepts-digest.md).
- The end-user declaration surface the facts layer carries: [`dblect_technical_intro.md`](./dblect_technical_intro.md) and [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md).
- The current uniqueness facts module: [`../../src/dblect/uniqueness/facts.py`](../../src/dblect/uniqueness/facts.py).
- Foundational literature. Abstract interpretation (Cousot and Cousot) is the framework this engine is an instance of, and the source of the monotone-transfer and sound-over-approximation obligations. Provenance semirings (Green, Karvounarakis, Tannen 2007) and functional-dependency propagation (Abiteboul, Hull, Vianu) supply the algebra's shape for the counting and accumulating properties; aggregate provenance (Amsterdamer, Deutch, Tannen 2011) is why aggregation gets its own transfer slot with a commutation obligation rather than riding the bare lattice. The why-provenance and hypothetical-query line (Karvounarakis and collaborators) is the model for evaluating one derivation under different world assignments. The type-qualifier tradition (CQual, FlowCaml) is the closest analogue for the user-domain lattice, and the gradual-typing tradition (Siek and Taha; Wadler and Findler on blame) for the typed/untyped seam. SQL formal semantics (HoTTSQL, Cosette) underpins the operator rules; Pandera and Pydantic shape the declaration surface.
- Issue [`#26`](https://github.com/dvryaboy/dblect/issues/26): promotes the demo nullability and aggregation-depth properties. Issue [`#16`](https://github.com/dvryaboy/dblect/issues/16): multi-source uniqueness detectors consume the substrate.
