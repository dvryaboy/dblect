# Lineage facts: grounding annotations from declarations

Status: design
Audience: engineers working on the lineage substrate, on a `Property` that needs values from manifest declarations or developer assertions, or on the flag system that feeds configuration values into propagation. It assumes the propagation calculus from [`propagation-soundness.md`](./propagation-soundness.md) (how a property propagates, and the obligations it meets) and the engine from [`column-level-lineage.md`](./column-level-lineage.md). This doc covers one thing: how declarations become grounded values that enter the walk, and the soundness contract for that grounding.

## Motivation

The substrate from [`column-level-lineage.md`](./column-level-lineage.md) gives every property a graph to propagate through. It does not say where values *enter* the graph. Each property has to invent its own grounding, and today the demo properties hard-code constants (`UNKNOWN` for nullability, `0` for aggregation depth) because there is no shared way to read `not_null` tests, declared column types, native constraints, candidate keys, or developer refinement declarations off the manifest.

The capability this unlocks: a developer declares a refinement (say `RevenueNet` on `fct_orders.order_total`, or a candidate key on `dim_customer`) on the model where the meaning lives. The framework propagates the claim downstream as the contract callers can rely on, and checks it against the SQL that produces the model. Without a shared facts module, every property that wants such grounding reimplements manifest plumbing, picks its own precedence rules, and tests its own discovery code.

A `lineage.facts` module makes this a substrate concern. A fact can be about one column or a whole relation, and the substrate treats both uniformly. The uniqueness layer migrates onto it as one property, and the same module is the bridge to the flag system when a config or var carries a refinement.

It is also the convergence point for the three authoring channels the rest of the design relies on: dbt tests and constraints, `meta.dblect.*` blocks in `schema.yml`, and the Python `SemanticType` / `ModelContract` declarations from [`dblect_technical_intro.md`](./dblect_technical_intro.md). The substrate carries all three without exposing any of its machinery to the people writing the declarations.

## How grounding fits the walk

The shape of propagation, in one paragraph; the calculus and its soundness are in [`propagation-soundness.md`](./propagation-soundness.md). The **propagator** walks the lineage graph once per property in dependency order and produces an `Annotation` for every node. At a node with a derivation it reduces the node's expression by applying the property's per-operator transfer rules. At a node with no derivation (a source or a seed) it reads the starting value from facts. This module is that read: it grounds each node from the declarations the manifest and the dblect surface carry. The walk stays single-pass because the lineage graph is acyclic once recursive-CTE and window regions are treated as opaque boundaries (a recursive CTE needs a fixpoint, so it stays opaque; a window is row-preserving and a later phase narrows the cut, see [`window-propagation.md`](./window-propagation.md)).

Two postures carry everything below, and both are facts concerns:

- **Annotations degrade, they never lie.** A degraded annotation is top-shaped, never a wrong precise value. When nothing grounds a node and the SQL reveals nothing, the propagator emits the lattice top and stays silent: a finding there would be a guess. When a recognized operation clears a *declared* refinement, the propagator names the cause and reports it (the seam and coherence cases under "Validation and propagation").
- **Facts must be rock-solid**, because detectors rely on them silently. A wrong fact produces a wrong annotation produces a false-positive finding; an absent fact produces a missing annotation produces a silent skip. The audit is louder when it knows and quieter when it does not.

## What a fact is

A **fact** is a typed claim about one node of the lineage graph, under one property, with provenance. A node is either a column or a relation, and those are the only two subjects a fact can have. Anything that looks multi-column is a relation fact whose *value* names the columns: a candidate key `{customer_id, region}` is the statement "this relation is unique on `{customer_id, region}`," so it attaches to the relation and the column set lives in the value, never in the address.

A fact grounds a node in one of two ways, depending on whether the node has a derivation:

- **Anchoring.** No derivation (a source or seed column, or the source relation itself). The fact is the only input the propagator has.
- **Asserted.** The node is derived (a model output column, a model's candidate key emerging from a `SELECT`). The fact is a developer or contract claim about what the derivation produces. The propagator uses it forward and checks it against the upstream.

## Data model

The data model makes illegal states unrepresentable. A fact is parameterized by both its value type and its scope kind, so a column property cannot be handed a relation fact. Provenance is a sealed union, so a field that is meaningful only for one kind of fact exists only on that kind.

```python
from typing import Any, Callable, Collection, Generic, Hashable, Mapping, Protocol, TypeVar, final, runtime_checkable
from dataclasses import dataclass, field
from enum import StrEnum

from dblect.lineage.graph import ColumnRef, SourceRef
from dblect.lineage.expr import Expr            # the sqlglot expression wrapper
import sqlglot.expressions as exp

K  = TypeVar("K")
K2 = TypeVar("K2")
S  = TypeVar("S",  ColumnRef, SourceRef)   # a property is column- OR relation-scoped, never both
S2 = TypeVar("S2", ColumnRef, SourceRef)

# A world assignment chosen by the flag layer. Opaque to the substrate in meaning;
# facts bucket by world, so its identity must be stable (hashable, value equality).
@dataclass(frozen=True, slots=True)
class WorldRef:
    assignments: frozenset[tuple[str, Hashable]]


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

### One annotation, one way to be unknown

The propagator stores and passes an `Annotation`, never a bare `K`. The value rides with a single tag, `opacity`, that answers one question: when a value is the lattice top, is that top *chosen* or *incidental*? That is the whole of "how we fail to know," and it reads the same whether the top arises at a leaf (grounding) or mid-walk (a clearing operator), which is why there is one vocabulary for it rather than a separate type at each site.

```python
class Opacity(StrEnum):
    REFINED  = "refined"   # value carries information (value is not top)
    EXPLICIT = "explicit"  # value is top by a declared opt-out; flows silently
    IMPLICIT = "implicit"  # value is top incidentally (nothing declared it); warns at a refinement seam


@dataclass(frozen=True, slots=True)
class Annotation(Generic[K]):
    value:   K
    opacity: Opacity = Opacity.REFINED
    provisional: bool = False   # error-recovery taint, orthogonal to opacity
```

`opacity` carries information only when `value` is top: `REFINED` *is* "value is not top," and the choice that matters (`EXPLICIT` versus `IMPLICIT`) is whether a top was chosen or fell out. `provisional` is the one bit that is not about knowing or not knowing: it is an error-recovery taint, set when a node's inferred value conflicts with its declared value, propagated as the logical OR of a transfer's inputs, and cleared when a node is freshly anchored by a fact the inferred value is consistent with. Detectors may downgrade findings that rest on a provisional annotation, and it never licenses a more precise value. It stays distinct from `opacity` on purpose. `enforced_on_write` (does a running guard back this constraint), `CompileOrigin.COMPUTED` (can the flag layer enumerate worlds over this value), and `provisional` are three separate axes, and none of them is a kind of unknown: collapsing them into the opacity vocabulary would lose exactly the distinctions the diagnostics rely on.

### Grounding returns a declared annotation

Grounding a node yields its **declared annotation**: the value and opacity the node carries before the walk combines anything into it. It is an ordinary `Annotation[K]`, so "anchored to a value," "declared opaque," and "no declaration" are the three `Opacity` cases of one type rather than a second three-way sum. The distinction that is load-bearing for the seam diagnostic, opt-out versus un-annotated, is exactly `EXPLICIT` versus `IMPLICIT`.

| grounding outcome | declared annotation | meaning |
|---|---|---|
| a fact resolved at the scope | `Annotation(value, REFINED)` | anchor or assert this value |
| the scope is opted out | `Annotation(top, EXPLICIT)` | declared opaque; flow top, silently |
| neither | `Annotation(top, IMPLICIT)` | nothing declared; the walk defaults to top |

An opt-out is still not a fact: a discoverer never emits a top-valued fact, so "declared opaque" is synthesized as a top-`EXPLICIT` annotation by the grounding builder rather than stored as a fact. Its input is an `OpaqueReader`, which reads the same three authoring channels a fact comes from (a `meta.dblect.opaque` key, an `OpaqueEffect` on a contract, an inline `dblect: opaque` marker) and returns the scopes opted out; the builder consults it before facts:

```python
class OpaqueReader(Protocol[S]):
    def opaque_scopes(self, manifest: "Manifest", *, name_to_source: Mapping[str, SourceRef]) -> Collection[S]: ...
```

The propagator's control flow falls out of the declared annotation without a separate type. A node with no derivation (a source or seed) flows its declared annotation directly. A node whose declared annotation is `EXPLICIT` short-circuits: it flows top silently and the walk is skipped, because the modeler took responsibility for the node. Otherwise the node is derived, the walk produces the **inferred** annotation, and validation (below) reconciles the two.

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
    proved something at least as precise. Derived from ``refines``, never hand-written.

    ``bottom`` is handled explicitly rather than left to ``refines``: ``bottom`` refines
    every value, so without this arm an inferred contradiction would pass vacuously. An
    inferred ``bottom`` means propagation derived a contradiction at this node, which is a
    finding, not a silent pass."""
    def check(declared: K, inferred: K) -> bool:
        if inferred == lat.bottom:
            return False
        return inferred == lat.top or lat.refines(inferred, declared)
    return check
```

The three property shapes instantiate the one lattice:

| Property            | `top`       | `x` refines `y` when …            | `meet` (resolve)        | `join` (confluence)              | `bottom` reachable |
|---------------------|-------------|-----------------------------------|-------------------------|----------------------------------|--------------------|
| Nullability         | `UNKNOWN`   | `x` is a stronger non-null guarantee | the stronger guarantee | weaker (either-null is nullable) | no                 |
| Uniqueness          | `{}` (no keys) | `x` knows a superset of `y`'s keys | union of keys         | keys both branches carry         | no                 |
| User-domain axis (enum) | `UNKNOWN` | `x == y`, or `y` is `UNKNOWN`     | equal value, else `bottom` | equal value, else `UNKNOWN`   | yes                |

The user-domain row shows the simplest shape, an enum where any two distinct values disagree (`contains_tax`, currency). An axis is free to use any bounded lattice instead: an interval for a range (`meet` is intersection, `join` is the hull, `bottom` is the empty interval), a value set for accepted-values, or a chain where one value genuinely refines another (`daily` under `monthly` under `yearly`). All of them go through the same `resolve` and `consistent`; only the `meet`, `join`, `top`, and `bottom` differ.

A genuine contradiction is `meet == bottom`. For an enum axis it is reachable whenever two declarations name different values (a contract says `contains_tax=TRUE`, a meta block says `FALSE`); for an interval axis, whenever two declared ranges do not overlap. Structural properties never contradict: a `not_null` constraint and a permissive `nullable: true` declaration meet to the stronger guarantee, and two candidate-key declarations simply union.

### Properties, transfers, and discoverers

A `Property` bundles the lattice with the transfer catalogs and the grounding function. The transfer semantics and their soundness obligations are in [`propagation-soundness.md`](./propagation-soundness.md); what follows is the substrate shape facts plugs into.

```python
_MINT = object()   # module-private token; only this module can mint a PropertyRef


@final
@dataclass(frozen=True, slots=True)
class PropertyRef(Generic[K2, S2]):
    """A typed handle to a property, minted once as a property's own ``ref``. ``K2`` and
    ``S2`` are the property's real value and scope types, so a read site recovers them
    rather than ``object``. The handle is un-forgeable: its constructor requires a
    module-private mint token, so a caller cannot build a ``PropertyRef[WrongK, S]`` with
    chosen parameters and read another property's annotation back at the wrong type.
    Equality is on ``name`` (the registry rejects duplicates), and the registry checks a
    ``depends_on`` edge against the *identity* of a registered property's minted ref, so a
    forged handle fails assembly rather than silently mistyping a read. Soundness no longer
    rests on the convention 'never hand-construct this'; the constructor enforces it."""
    name: str
    _mint: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TypeError("PropertyRef is minted by Property, not constructed directly")


class DepContext(Protocol):
    def annotation(self, ref: PropertyRef[K2, S2], scope: S2) -> Annotation[K2] | None: ...


# Transfers receive and return annotations, so opacity and the provisional taint flow
# through them. A property with no dependencies ignores the DepContext.
OperatorTransfer = Callable[[Expr, tuple[Annotation[K], ...], DepContext], Annotation[K]]


@dataclass(frozen=True, slots=True)
class CoherenceGuard:
    """A precondition an aggregate's meaning rests on: the ``within`` columns must be
    constant across each aggregated group, which is the functional dependency
    ``group_keys -> within``. The guard reads that FD from the dependency property ``fd``
    at the aggregation's input relation; where it does not hold the aggregate's result
    clears to top and the seam rule reports it, because a declared refinement was lost.
    Compiles from a types-layer ``within=<cols>`` declaration."""
    fd:     PropertyRef[Any, SourceRef]   # the functional-dependency / uniqueness property to read
    within: tuple[str, ...]               # the columns required constant within the group


@dataclass(frozen=True, slots=True)
class AggregateRule(Generic[K]):
    """An aggregate transfer split so its soundness obligation is checkable. ``core`` is a
    pure value-domain map with no DepContext, and it is the piece that must commute with
    confluence and cross (see propagation-soundness.md). ``coherence`` is the optional
    clear-on-failure guard, and the FD read it performs is the only way a dependency enters
    an aggregate, so ``core`` is property-tested in isolation."""
    core:      Callable[[exp.AggFunc, Annotation[K]], Annotation[K]]
    coherence: CoherenceGuard | None = None


class FactDiscoverer(Protocol[K, S]):
    """Reads the manifest and dblect declarations, returns facts for any node it can
    ground. Pure, and it returns a materialized collection so that a discoverer which
    raises drops all of its facts and none of another's."""

    def discover(
        self, manifest: "Manifest", *, name_to_source: Mapping[str, SourceRef],
    ) -> Collection[Fact[K, S]]: ...


# The operator algebra (dblect.lineage.semiring.Semiring) is an optional slot, present
# only for a property whose confluence or cross counts or accumulates; left unset for the
# idempotent and value-domain properties this module ships. See propagation-soundness.md
# for what the semiring buys and column-level-lineage.md for the existing instances.
@runtime_checkable
class Semiring(Protocol[K]):
    @property
    def zero(self) -> K: ...   # identity for plus, annihilator for a strict times
    @property
    def one(self) -> K: ...    # identity for times
    def plus(self, a: K, b: K) -> K: ...    # confluence combine (UNION ALL)
    def times(self, a: K, b: K) -> K: ...   # cross combine (JOIN)


@dataclass(frozen=True, slots=True)
class AxisDisplay:
    """The human-facing names the seam diagnostic fills its template from. Reserved
    here; the types layer supplies it from a declaration, with fallback to the bare
    type and axis names."""
    name:        str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class Property(Generic[K, S]):
    ref:        PropertyRef[K, S]                 # the property's own typed handle, minted once; name lives here
    scope_kind: ScopeKind                         # runtime walk dispatch; the smart constructors fix it to match S
    lattice:    Lattice[K]                        # abstraction domain: resolve and consistent only
    operators:  Mapping[type[Expr], OperatorTransfer[K]]
    aggregates: Mapping[type[exp.AggFunc], AggregateRule[K]]
    ground:     Callable[[S], Annotation[K]]      # the node's declared annotation (REFINED / EXPLICIT / IMPLICIT)
    semiring:   Semiring[K] | None = None         # operator algebra for counting/accumulating properties
    display:    Callable[[K], AxisDisplay] | None = None   # seam-diagnostic names; None falls back to type/axis names
    depends_on: tuple[PropertyRef[Any, Any], ...] = ()
```

Two invariants hold the optional `semiring` slot together with the rest. When it is set, the relational operators (`Union`, `Join`) are derived from `plus`/`times` and must not be redefined in `operators`, and an idempotent semiring must satisfy `plus == lattice.join` so confluence has one answer. Both are checked once at construction. `consistent` and `resolve` are derived from `lattice`, so they are not fields. Two smart constructors, `column_property` (fixing `scope_kind=COLUMN`) and `relation_property` (fixing `RELATION`), set `scope_kind` from the scope type so the field cannot drift from `S` in practice; the field stays for runtime walk dispatch, where the erased `S` is not available.

A property names the properties its transfers read in `depends_on`, an edge that both sets evaluation order and types the read: a transfer reaches a dependency only through the read-only `DepContext`, and a transfer that did not declare an edge cannot type a read of that annotation, so a missing edge is a type error at authoring time. The typed read is sound because a `PropertyRef` cannot be hand-constructed (the mint token enforces it), the registry rejects duplicate names and checks each edge against a registered property's ref by identity, and a relation-scoped read from a column node derives its scope from the column's own `ColumnRef.source`. `DepContext.annotation` returns `None` when the dependency is silent at that scope, which a transfer reads as the dependency's lattice top, the same "we don't know" every other absence means. The acyclicity, ordering, and monotone-in-the-dependency obligations are in [`propagation-soundness.md`](./propagation-soundness.md); the user writes none of it, since the edge originates in a declaration (the coherence edge on a `Money` type) the framework compiles.

A constructor wires a property from its discoverers:

```python
def nullability_property(
    manifest: "Manifest", *, name_to_source: Mapping[str, SourceRef],
    extra: tuple[FactDiscoverer[Nullability, ColumnRef], ...] = (),
) -> Property[Nullability, ColumnRef]:
    facts = collect(manifest, (*_NULLABILITY_DISCOVERERS, *extra), name_to_source=name_to_source)
    return column_property(
        name="nullability",
        lattice=NULLABILITY_LATTICE,
        operators=_NULLABILITY_OPERATORS,
        aggregates=_NULLABILITY_AGGREGATES,
        ground=grounding(facts, _NULLABILITY_OPAQUE(manifest), NULLABILITY_LATTICE),
    )
```

`grounding` turns the collected facts into the per-node lookup: it folds a scope's bucket through `resolve`, raises a `BuildIssue` on a `bottom` contradiction, and returns the declared annotation, `Annotation(top, EXPLICIT)` for a scope in the opaque-opt-out set, `Annotation(value, REFINED)` where a value resolved, and `Annotation(top, IMPLICIT)` otherwise.

```python
def collect(
    manifest: "Manifest", discoverers: tuple[FactDiscoverer[K, S], ...],
    *, name_to_source: Mapping[str, SourceRef],
) -> Mapping[S, tuple[Fact[K, S], ...]]:
    """Run each discoverer and bucket its facts by scope. A discoverer that raises a
    DiscovererError contributes nothing and the others are unaffected; any other
    exception is a substrate bug and propagates, failing the build loudly rather than
    silently dropping facts."""

def grounding(
    facts: Mapping[S, tuple[Fact[K, S], ...]], opaque: Collection[S], lat: Lattice[K],
) -> Callable[[S], Annotation[K]]: ...
```

The errors are a small sealed set. `BuildIssue` is raised by resolution when a scope's facts meet to `bottom`; it carries the scope and the conflicting facts, is collected and reported rather than aborting, and the run continues from the deterministic `bottom`-derived value with downstream annotations marked provisional. `SeamContradiction` is raised by the binary `combine` when two committed operands are incompatible; it becomes a finding at the combine site. `DiscovererError` is the only exception `collect` treats as expected, isolating one discoverer's failure from the rest.

## Resolving multiple facts at a scope

Several discoverers can ground the same node. `resolve` folds the bucket with the lattice meet, which is the most precise value consistent with every claim. Because meet is associative and commutative, the result is independent of discoverer registration and dict iteration order. A `bottom` result is a genuine contradiction: the build surfaces a `BuildIssue`, keeps the deterministic `bottom`-derived value so the run reproduces, and marks downstream annotations provisional. Provenance stays on each fact for tracing and reporting, and never enters resolution.

**Compile-value facts share one world.** The flag layer fixes one world per propagation run, and the compile-value discoverers emit their facts under it, so every fact in a bucket shares that world and resolution is ordinary. A var-derived value is ground truth in its world, the same standing as a native constraint or a user assertion. A difference *between* worlds is the flag-world analysis ([`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md)), reported as "this contract holds under world A and fails under world B." Where a world-scoped value and an unconditional assertion genuinely disagree on one axis (a contract claims `contains_tax=TRUE` always, world B produces `FALSE`), that disagreement is the finding the analysis exists to raise. The audit exposes a "trace this annotation to its grounding facts" helper that reconstructs a derivation on demand.

## Discovery

A discoverer per axis. The substrate ships discoverers for the axes production properties need first; user properties register their own. A discoverer is pure and total within its axis: every node it claims authority over either gets a fact or is silently skipped. It never emits a top-valued fact pretending to be a claim; an explicit opaque opt-out is its own declaration, surfaced as a top-`EXPLICIT` declared annotation rather than as a value.

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

All three authoring channels reduce to a `Fact`, and a developer writing a declaration never meets `Lattice`, the `Opacity` tag, or the transfer catalogs.

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

## Assembling a run

A property is not free-floating: it joins a run through a registry, which is the seam a developer-defined refinement enters by. The audit builds one `PropertyRegistry` per run from the built-in properties plus any contributed by the types layer (a compiled `Money` property is one more entry, indistinguishable from a built-in once registered).

```python
@dataclass(frozen=True, slots=True)
class PropertyRegistry:
    properties: tuple[Property[Any, Any], ...]

    def evaluation_order(self) -> tuple[Property[Any, Any], ...]:
        """Topological order over depends_on. Raises on a cycle, on a duplicate name, or
        on an edge whose ref is not the minted ref of a registered property (an identity
        check, not a name match, so a forged handle fails here)."""

    def dep_context(self, store: "AnnotationStore") -> DepContext:
        """A read-only view of the annotations computed so far, keyed by (name, scope)."""
```

Name uniqueness plus ref identity is what makes the typed dependency read sound: a `PropertyRef` is minted only inside `Property`, a name maps to exactly one registered property, and a `depends_on` edge is checked against the minted ref by identity, so `dep_context(...).annotation(ref, scope)` returns the right value at the right type and a forged handle cannot mistype the read. Ordering is automatic: a user property declares `depends_on` on a built-in property's `ref` and `evaluation_order` interleaves it with the built-ins, so the author writes no ordering. A cycle or a dangling edge is a build error checked once at assembly, which is the acyclic guarantee the single-pass walk rests on. The propagator runs each property in `evaluation_order`, accumulating annotations into the store the next property's `DepContext` reads.

## Validation and propagation

At a node the propagator has up to two inputs: the **inferred** annotation, from walking the upstream expression (absent at sources and seeds), and the **declared** annotation, from `ground` (top-`IMPLICIT` when nothing is declared, top-`EXPLICIT` for an opt-out, a `REFINED` value where a fact resolved). Two independent decisions follow.

**Validation** runs `consistent(declared, inferred.value)`. It holds when the SQL revealed nothing (`inferred` is top) or proved something at least as precise as the declaration. A property never overrides this; it is derived from the property's lattice, so it cannot drift from the order that resolution uses.

**Propagation** decides what flows onward. The node carries one propagating annotation, the **flow** value: the most precise value the framework can justify, where "justify" means every step from the declared inputs to here preserved or combined the value (a theorem or a user signature) rather than clearing it. A declared node additionally pins a **boundary** value, the value it publishes to other models. The boundary is not a second propagating lattice; it is the contract a model exposes at its edge. Within the model, downstream nodes read the flow value. When a downstream model references this column, it anchors on the boundary value if one was declared, otherwise on the flow value, so a consumer that built against a deliberately loose contract is insulated from internal tightening.

| inferred                        | declared | consistent     | flow (within model)   | boundary (exported) | finding |
|---------------------------------|----------|----------------|-----------------------|---------------------|---------|
| absent                          | absent   | n/a            | top (`IMPLICIT`)      | none                | none |
| present                         | absent   | n/a            | inferred              | inferred            | none |
| absent                          | present  | n/a            | declared              | declared            | none (anchors a source) |
| top, `EXPLICIT` opt-out         | present  | yes (vacuous)  | declared              | declared            | none (opacity declared) |
| top, `IMPLICIT`                 | present  | yes (vacuous)  | declared              | declared            | typed layer: "guarantee unverified" (seam rule) |
| refines declared                | present  | yes            | inferred              | declared            | soft "can tighten" if strictly more precise |
| conflicts                       | present  | no             | declared (provisional)| declared            | hard finding |

The two rows that carry the design:

- **`refines declared` (tightening).** The SQL proves something at least as precise as the declaration, so the flow value is the inferred one. For a structural property this is unconditional (a `COALESCE` makes the column non-null whatever the declaration said); for a user-domain property it tightens only through preserving steps. The boundary stays at the declared value, so external consumers are unaffected and a developer keeps the right to publish a deliberately loose contract. When the inferred value is strictly more precise, the audit emits a suppressible "you can tighten this, or confirm the looseness is intentional," softer for user-domain axes where deliberate abstraction is common.
- **`conflicts` (violation).** The inferred value contradicts the contract. The audit raises a finding at the violation site, propagation continues from the declared value, and downstream annotations are marked `provisional`. This is error recovery: once the error is reported, assume the declared value so one upstream regression does not blank analysis of every consumer.

### Erasure at the typed/untyped seam

A refined value meeting an unrefined one is where the highest-value bugs hide and where a partial adopter most wants a nudge. dblect follows the gradual-typing tradition here (see references): separate an explicit opt-out from an absent annotation, and treat them oppositely. The `Opacity` tag carries exactly this distinction through transfers, so the binary combine can decide whether to speak:

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

A top the modeler *declared* (`EXPLICIT`) flows silently, because the modeler took responsibility. A top that is merely *un-annotated* (`IMPLICIT`), where it clears a declared refinement, makes the audit speak up; the diagnostic is on once a project has declared semantic types and off at the zero-declaration layer, so the signal lands where the investment already is. The same rule covers any clearing of a declared refinement, including an aggregate whose coherence precondition is not met (the mixed-currency `SUM`).

The diagnostic is a fixed template, not synthesized prose: the substrate does not know what a user-domain axis means, so it fills slots from the site, the operator, the two operand columns and their types, the axis that cleared, and the suppression path. The only domain-flavored text is a name the modeler chose, drawn from the property's `display` slot, with fallback to the bare type and axis names. The types layer fills that slot (see [`semantic-types-layer.md`](./semantic-types-layer.md)); the substrate plumbs it and never authors the text. A realistic rendering:

> `orders.sql:12`: `total` combines `revenue` and `net_revenue` with `+`. `net_revenue` is `RevenueWithTax` but `revenue` carries no refinement on `contains_tax`, so the result drops it. Annotate `revenue` as `RevenueWithTax` if it qualifies, or treat this as a possible mismatch. To silence: mark `revenue` opaque, or disable `refinement-erased-at-seam` for this model.

The runtime layer is the check at the seam: the static side notes the boundary, and the generator probes whether the unrefined side actually respects the refined side's assumption.

## Soundness contract

The general transfer obligations (sound, monotone, plan-independent, deterministic) live in [`propagation-soundness.md`](./propagation-soundness.md). This is the facts-specific layer on top.

1. **Discoverer correctness is a hard guarantee for the input it reads.** A discoverer that emits a fact its declaration does not support is a substrate bug. PBT covers each shipping discoverer. Whether the resulting conclusion is unconditional depends on what it rests on: one built only from core transfers is a theorem given the declared inputs; one that uses a user signature holds given the declared inputs and that signature.
2. **Absence is silence.** A node nothing declares grounds as a top-`IMPLICIT` declared annotation, the propagator returns the lattice top, and detectors read it as "we don't know."
3. **Conditional facts are captured but not activated.** A `not_null` or `unique` with a `where` filter produces a fact carrying the predicate, and grounding ignores the predicate for now. Activation follows the rule [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md) commits to, so the deferral is engineering sequencing rather than an open question.
4. **Contradictions are resolved and surfaced.** Two declarations whose values meet to `bottom` raise a `BuildIssue`; resolution keeps a deterministic value and never picks a winner from provenance. An inferred value that reaches `bottom` during propagation fails `consistent` and is a finding, never a vacuous pass.
5. **Facts cross model boundaries only through propagation.** The flow value carries downstream through the lineage graph; the boundary value gates cross-model contract checks.
6. **Asserted facts are checked, and the boundary is stable.** A fact on a derived node runs through `consistent` against the inferred value. A mismatch is a finding. The declared value remains the contract callers built against, and downstream-of-violation annotations are provisional.

## Trusting unenforced constraints

Leaf facts are conditional bets, the conceptual point made in [`propagation-soundness.md`](./propagation-soundness.md): a candidate key or `not_null` claim is an assertion the framework cannot verify against data, so a constraint the warehouse declares but does not enforce can make a propagated annotation wrong while the rules that produced it stay sound. Whether a native constraint actually backs its claim is an adapter-and-constraint-kind question (Databricks enforces `CHECK` but not `PRIMARY KEY`, Snowflake enforces neither), captured by `enforced_on_write` on the `NativeConstraint` provenance. The question that matters is whether a running guard exists, which is why provenance carries no authority order. Two things follow:

- **Discoverers are adapter-aware about enforcement.** The native-constraint discoverer knows the active adapter and sets `enforced_on_write` on each fact. This is descriptive provenance; resolution never reads it.
- **The runtime layer is the backstop, and the gap gets a finding.** The finding is scoped to constraints that actually carry weight, not every advisory constraint. A constraint-derived annotation is **load-bearing** when at least one reported conclusion depends on it: a finding it suppressed (the Duplicate detector stayed silent because the key was assumed unique), a boundary check it let pass, or a finding it raised. Operationally, it is load-bearing when dropping the constraint to top would change what the audit reports, which the "trace this annotation to its grounding facts" helper makes computable. Where such an annotation rests on a native constraint with `enforced_on_write=False` that no running test covers at the same scope, the audit emits a finding ("uniqueness on `dim_customer.id` rests on an advisory `PRIMARY KEY` and no `unique` test guards it; add a test"). The suppressed-finding case is the important one, since that is the silent false negative the advisory constraint can hide.

## Coverage and degradation

Silent degradation is sound but it can hide behind itself: a manifest where sqlglot resolves few columns produces few annotations and few findings, which can read as a clean bill rather than as thin coverage. The audit treats coverage as a first-class output, and keeps two metrics separate because they mean opposite things.

- **Resolution coverage** is the fraction of columns whose lineage the propagator could follow against the fraction it fell blind on (sqlglot could not resolve the column, a macro escaped rendering, a dialect construct misparsed). Blindness is a capability gap, so a configurable floor turns sustained blindness into a finding ("resolved 38% of columns on `fct_orders`; analysis below covers only what was resolved"). The floor keys on resolution only.
- **Grounding coverage** is, among resolved columns, how many a fact grounded, reported per discoverer. An ungrounded column is the expected case under "absence is silence," not a defect, so grounding coverage never trips a floor on its own. Where it earns a finding is scoped to declared intent: of the columns a contract names, how many resolved to a checkable annotation. That number tells a partial adopter whether their declarations are actually being checked, and it does not fire in the zero-declaration layer where ungroundedness is the whole point.

Separating the two keeps the floor from reporting thin coverage in exactly the adoption mode the design courts, where most columns legitimately carry no fact. The default posture stays silent-on-blindness for individual nodes; the floor is about the aggregate.

## What this does not cover

- **Uniqueness as the first relation-scoped property.** Uniqueness is the worked example for relation-scoped facts (`Property[CandidateKeySet, SourceRef]`, value is a candidate-key set, discoverers map `unique` / `unique_combination_of_columns` / native `PRIMARY KEY` to relation facts). Its migration onto this substrate is its own change, tracked in [`#16`](https://github.com/dvryaboy/dblect/issues/16), and its relation-algebra walk and key/FD plan-independence are detailed in [`column-level-lineage.md`](./column-level-lineage.md) and [`propagation-soundness.md`](./propagation-soundness.md).
- **Activation of conditional facts.** See [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- **World enumeration over flag values.** Belongs to the flag system. This module supplies values inside a world; the flag layer chooses worlds and compares evaluations across them.
- **Cross-package fact inference.** Facts declared in a dbt package and consumed by a downstream package that does not import it. Same scope cut as [`var-inference-spec.md`](./var-inference-spec.md).
- **Runtime facts from the warehouse.** `INFORMATION_SCHEMA` or adapter-side metadata. Lands when an adapter-aware fact source is requested.
- **Inference from SQL.** A column projected as `COALESCE(x, 0)` grounds a nullability annotation through the property's operator rules, not through a fact. Facts are declarations; inference is the propagator's job.
- **Recursive-CTE propagation.** Treated as an opaque boundary that re-anchors on output, because a recursive CTE needs a fixpoint the single-pass walk does not run.
- **Window propagation.** This substrate re-anchors at window outputs for now; a window is row-preserving, so a later phase narrows the cut. The design is in [`window-propagation.md`](./window-propagation.md).

## Testing

- **Per-discoverer PBT.** Generate manifests and declarations with random metadata; assert each discoverer's facts are a function of its documented input, never invent claims, never drop ones they should produce, and never emit a top-valued claim.
- **Lattice laws.** PBT on each property's lattice (associativity, commutativity, idempotence of meet and join, absorption, the `top`/`bottom` identities) and on the derived `consistent` (reflexivity, and `consistent(declared, top)` for every value so an opaque upstream never fails the check). Because resolution and `consistent` are derived from the lattice, this is the single place those laws are tested.
- **Resolution determinism.** A bucket of facts in any order resolves to the same value; a `bottom` contradiction raises a `BuildIssue` and yields the same deterministic value regardless of order. Compile-value facts sharing one `WorldRef` bucket by world equality, so resolution within a world is order-independent.
- **Opaque grounding.** A scope in the opaque-opt-out set grounds as a top-`EXPLICIT` declared annotation rather than `REFINED` or `IMPLICIT`, regardless of any facts also present, and flows silently.
- **Dependency-read soundness.** A registry with a duplicate property name, a `depends_on` cycle, or an edge whose ref is not a registered property's minted ref fails assembly; a `DepContext` read returns the dependency's annotation at the recovered type, and a silent dependency reads as top.
- **Seam diagnostic.** An `EXPLICIT` top meeting a declared refinement is silent; an `IMPLICIT` top meeting one is silent at the zero-declaration layer and a finding at the typed layer; two committed incompatible operands are a finding at both. The diagnostic names the column, both readings, and the suppression path.
- **Tightening and boundary.** A structural property whose inferred value is strictly more precise than the declaration propagates the inferred value as flow, keeps the declared value as boundary, and emits the soft finding. A user-domain property does the same through a preserving chain, and a clearing step stops the tightening.
- **Asserted-fact end-to-end.** A `not_null` declaration on a column with a `NULLABLE` upstream surfaces a finding and propagates the declared value downstream as provisional; the same with a `NON_NULL` or top upstream propagates without a finding. The analogous test for a candidate-key declaration on a derived model.
- **Coverage reporting.** A deliberately under-resolvable model reports low resolution coverage and trips the floor finding; a fully resolvable model with no declarations reports full resolution coverage and low grounding coverage and trips no floor, so absence-is-silence does not read as thin coverage.

The transfer, aggregate-commutation, semiring-law, and walk-determinism obligations are tested against [`propagation-soundness.md`](./propagation-soundness.md)'s checklist, shared by every property rather than specific to facts.

## References

- The propagation calculus and its soundness obligations: [`propagation-soundness.md`](./propagation-soundness.md).
- The engine this layers on: [`column-level-lineage.md`](./column-level-lineage.md), including the K-relations encoding for uniqueness.
- The end-user declaration surface the facts layer carries: [`dblect_technical_intro.md`](./dblect_technical_intro.md), [`semantic-types-layer.md`](./semantic-types-layer.md), and [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md).
- The current uniqueness facts module this migration evolves: [`../../src/dblect/uniqueness/facts.py`](../../src/dblect/uniqueness/facts.py).
- Issue [`#26`](https://github.com/dvryaboy/dblect/issues/26): promotes the demo nullability and aggregation-depth properties. Issue [`#16`](https://github.com/dvryaboy/dblect/issues/16): multi-source uniqueness detectors consume the substrate.
