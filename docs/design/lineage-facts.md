# Lineage facts: grounding annotations from declarations

Status: design
Audience: engineers working on the lineage substrate, on a `Property` that needs values from manifest declarations or developer assertions, or on the flag system that feeds configuration values into propagation. The first half is a tour of how property propagation works, so a reader new to the substrate can start here. It assumes SQL and a passing familiarity with semirings and lattices; it assumes nothing about provenance or type-theory literature, and the references collect the traditions it draws on.

## Motivation

The substrate from [`column-level-lineage.md`](./column-level-lineage.md) gives every property a graph to propagate through. It does not say where values *enter* the graph. Each property has to invent its own grounding, and today the demo properties hard-code constants (`UNKNOWN` for nullability, `0` for aggregation depth) because there is no shared way to read `not_null` tests, declared column types, native constraints, candidate keys, or developer refinement declarations off the manifest.

The capability this unlocks: a developer declares a refinement (say `RevenueNet` on `fct_orders.order_total`, or a candidate key on `dim_customer`) on the model where the meaning lives. The framework propagates the claim downstream as the contract callers can rely on, and checks it against the SQL that produces the model. Without a shared facts module, every property that wants such grounding reimplements manifest plumbing, picks its own precedence rules, and tests its own discovery code.

A `lineage.facts` module makes this a substrate concern. A fact can be about one column or a whole relation, and the substrate treats both uniformly. The uniqueness layer migrates onto it as one property, and the same module is the bridge to the flag system when a config or var carries a refinement.

It is also the convergence point for the three authoring channels the rest of the design relies on: dbt tests and constraints, `meta.dblect.*` blocks in `schema.yml`, and the Python `SemanticType` / `ModelContract` declarations from [`dblect_technical_intro.md`](./dblect_technical_intro.md). The substrate carries all three without exposing any of its machinery to the people writing the declarations.

## How propagation works

A **property** is a value type `K`, a lattice over `K`, and rules for moving a `K` through SQL. `K` is small and ordered by *precision*: a more precise value commits to more about the data. Nullability ranges over `{NON_NULL, NULLABLE, UNKNOWN}`; uniqueness over candidate-key sets `frozenset[frozenset[ColumnRef]]`, where knowing more keys is more precise; a user-domain axis ranges over whatever bounded lattice it needs (the smallest is a two-value axis like `contains_tax` over `{TRUE, FALSE, UNKNOWN}`; currency is an enum, an accepted range is an interval).

For each property, the engine gives every node a value by combining its inputs with one rule per SQL operator, the property's *transfer rules*. Two requirements keep this honest: a rule never reports a value more precise than the SQL guarantees (when in doubt it falls back to the lattice top, the only value it may emit without proof), and feeding it a more precise input never yields a less precise output. Those are what the references call a sound, monotone transfer, and together they make the whole walk safe to trust. The asymmetry is worth stating plainly: top is the safe default, but every value strictly below it is a positive claim. A `NULLABLE` is "I proved a null can occur here," not "I have not proved non-null," so a rule may emit it only where the SQL establishes it, never as a fallback on uncertainty.

Two structures do separate jobs here, and keeping them apart is what keeps the engine honest. The **abstraction domain** is the precision lattice: it powers fact resolution (the meet of several declared facts) and the consistency check (does an inferred value refine a declared one). The **operator algebra** is how values combine *at SQL operators*: the confluence combine at a `UNION ALL`, the cross combine at a `JOIN`. For an idempotent property the confluence combine is exactly the domain join, which is why the two look like one thing. They are not one thing in general. A counting property's confluence is addition, which is not the join of any lattice on counts. The cross combine is the property's own rule, not the domain join by definition: it multiplies for counting, preserves per side for the column properties shipped here, and only for a pure-accumulation property such as where-provenance, whose combine is set union throughout, does it happen to coincide with the join. The properties this module ships are all idempotent (nullability, uniqueness) or value-domain (type, accepted-values, range, the user axes), so their confluence is the domain join and their operator algebra needs no extra structure. The counting and accumulating properties whose confluence is a non-idempotent semiring `+` live on the semiring substrate of [`column-level-lineage.md`](./column-level-lineage.md); a `Property` carries an optional `semiring` slot for them, left unset here. The value-domain axes do their real work at the scalar transforms and aggregates (adding two revenue columns, a `COALESCE`, a `SUM` over a group), where a declared meaning is preserved, transformed, or lost; at a `JOIN` or `UNION` they simply ride their column through.

The **propagator** walks the lineage graph once per property in dependency order and produces an annotation for every node. At a node with a derivation it reduces the node's expression by recursing into upstream nodes and applying the property's per-operator transfer rules. At a node with no derivation (a source or a seed) it reads the starting value from facts. The walk is single-pass because the property dependency graph is acyclic (below) and the lineage graph is acyclic once recursive-CTE and window regions are treated as opaque boundaries. The two are not the same kind of cut. A recursive CTE needs a fixpoint the single-pass walk does not run, so it stays opaque. A window is row-preserving, so the structural properties can propagate through it soundly; this substrate re-anchors at window outputs for now to keep the first cut simple, and a later phase narrows it (see [`window-propagation.md`](./window-propagation.md)).

Two points carry everything below:

- **One engine, many properties, one pass each.** Adding a property is adding a `Property`, never a new pass. Properties are independent unless one explicitly declares a dependency on another; nullability never consults uniqueness.
- **Annotations degrade, they never lie.** A degraded annotation is `UNKNOWN`-shaped, never a wrong precise value. When sqlglot cannot resolve a column the propagator cannot see what the SQL did, so it emits the lattice top and stays silent: a finding there would be a guess. When a recognized operation clears a *declared* refinement the propagator can name the cause and reports it (the seam and coherence cases under "Validation and propagation").

### Two families of properties

Properties differ by where their transfer rules come from, and that difference is the whole of the structural/user-domain distinction. The engine does not branch on it.

- **Framework transfers** are theorems about SQL semantics, true in every project: a `JOIN` multiplies cardinality, `DISTINCT` introduces a key, `COALESCE(x, 0)` is non-null. Nullability and uniqueness ship here on these; cardinality, grain, and ordering are the same kind of proven-core property and live on the semiring substrate of [`column-level-lineage.md`](./column-level-lineage.md). They are the proven core, verified once.
- **User transfers** rest on declared signatures: whether `revenue * 0.9` preserves tax inclusion is what the author meant, which the framework cannot derive. Currency, tax inclusion, gross/net, and the other user-domain axes are built from these, in an open catalog users extend.

A finding carries the assumptions in its derivation, which the propagator traces: one built only from core transfers is unconditional, one that passes through a user signature holds given that signature. What has to be proved differs, the machinery does not (see "Soundness contract"): the framework proves its own rules; a user rule is correct as long as the author's declared signature behaves the way they say.

### Transfer rules by operator

A property's behaviour is indexed by relational operator. Most of it is forced by the lattice rather than chosen.

- **Filter / selection**: preserve. Forced.
- **Confluence (`UNION ALL`)**: the property's confluence combine. For the idempotent properties shipped here this is the domain join (nullability: nullable if either branch is; uniqueness: a key survives only if both branches carry it), so it is forced by the lattice. A counting property supplies it instead as the semiring `+` (addition), which is not a domain join; that case lives on the semiring substrate (see [`column-level-lineage.md`](./column-level-lineage.md)). `UNION` (distinct) is the same confluence with one operator-specific addition: set semantics dedups, so the whole projected row is a *superkey*, contributed to the key set only where no existing smaller key already subsumes it (the key set stays a minimal antichain, below). The confluence rule is therefore keyed on the specific operator: `UNION` and `UNION ALL` have different row semantics.
- **Cross (`JOIN`)**: each side preserves; a multi-input scalar expression folds its operands by the operator's rule. For a column-scoped property there is no cross-column combine to define at the join itself: each output column traces to exactly one input column, so the `JOIN` is projection and the only real combines a column property has are confluence and multi-input scalars. Plan-shape independence there reduces to those two combining associatively, with no distributivity obligation because there is no `×` to distribute. The genuine cross combine appears for relation-scoped uniqueness, which combines keys across sides subject to join-condition coverage, a side condition on the join predicate rather than on the two annotations alone. That makes it key propagation rather than a semiring `×`, and its plan-independence is proven directly with the uniqueness migration (below) rather than read off semiring laws. For a counting property the cross is the semiring `×` (multiplication), on the semiring substrate. For a pure-accumulation property such as where-provenance the `times` is set union, which coincides with the domain join.
- **Scalar / projection**: preserve, transform, or clear. A genuine choice. An identity (`Alias`, a bare `Column`) preserves and is where tightening happens; a declared map (a currency conversion, a `discount` or `tax` annotation) transforms; an opaque scalar or bare literal clears the value to the lattice top. A binary combine (`a + b`) preserves when operands agree on the axis, raises a finding when two committed operands are incompatible (tax-inclusive plus tax-exclusive), and clears to top when a committed operand meets an unrefined one, recording why it cleared so the seam rule can decide whether to speak.
- **Aggregation**: the aggregate transfer, whose behaviour is the measure's *combinability*. A genuine choice.

So a property chooses behaviour only at scalar transforms and at aggregation; the rest follows from the lattice.

The aggregate transfer asks whether a measure's meaning survives a `GROUP BY`, and under what precondition. Three outcomes cover it: **preserved** (a value-returning aggregate over a normal measure keeps its axes), **preserved under coherence** (it survives only where named columns are constant in the aggregation scope), or **cleared** (no aggregate preserves it, as for a ratio). An aggregate rule is two pieces so its soundness obligation stays checkable. Its `core` is a pure value-domain map, and that core is what must commute with confluence and cross, so the single-pass walk gives the same annotation regardless of whether an aggregate sits above or below a `UNION ALL`. Because the core takes no `DepContext` it is discharged in isolation: PBT'd directly for each shipped aggregate, required of user-supplied ones, and for a counting or accumulating property it is the semimodule homomorphism law of the aggregate-provenance tradition (the references), inherited once the property supplies its semimodule. The **preserved-under-coherence** outcome is not in the core at all; it is an optional `CoherenceGuard` that reads a functional dependency and clears to top where it fails. Clearing to top commutes with confluence trivially (top absorbs), so factoring coherence into the guard is exactly what lets the commutation law be stated over a pure function rather than over one that reads a dependency. The guard's plan-stability rests on the FD property it reads being itself plan-independent, which is the dependency the `depends_on` edge records. Aggregation is the one place the bare lattice does not force the rule, which is why it gets its own slot. The user-land vocabulary that compiles to these outcomes lives in the types layer; the v1 surface is a coherence declaration (`within=<cols>`, compiling to the guard) plus a `summable` flag (compiling into the core) for measures that never aggregate additively.

### Properties reading one another, in dependency order

Most properties propagate alone. A few need another property's annotations to compute their own transfers, and two cases carry the design.

*Cardinality reads uniqueness.* To tell a fan-out join from a key-preserving one, the cardinality transfer at a `JOIN` asks whether the join key is unique on the other side. That answer is the uniqueness property's annotation, read at the join node. Both come from the proven core.

*A user-defined money type reads a functional dependency.* Currency is not built in; it is a refinement a developer declares (a `Money` semantic type carrying a currency axis), which the types layer compiles to a property like any other. Take `SELECT region, SUM(amount) AS total FROM orders GROUP BY region`, where `amount` is typed `Money`. Does `total` keep its currency? Only if every row folded into a group already shares one, that is, only if `region -> currency` holds. The coherence declaration compiles to a `CoherenceGuard` on the aggregate rule that reads that functional dependency. Where it holds, currency is preserved; otherwise the framework cannot guarantee a group shares one currency, so the guard clears the axis and the audit flags it. The sum may be mixing currencies, which is the bug `within="currency"` exists to catch. It does not widen to top in silence, because losing a declared refinement is cause for investigation.

A property names the properties its transfers read in `depends_on`, and the propagator evaluates those first. A transfer reaches a dependency only through a read-only `DepContext` typed to the declared dependencies, never a shared global map. The edge is a wire: it sets evaluation order and is the sole channel for the read. A transfer that did not declare an edge cannot type a read of that annotation, so a missing edge is a type error at authoring time rather than stale state at runtime.

The typed read is sound because a `PropertyRef[K2, S2]` cannot be hand-constructed with chosen parameters: its constructor requires a module-private mint token, so the only handle that exists for a property is its own `ref`, minted once when the `Property` is built, and the `K2`/`S2` on it are the real value and scope types of that property. A `depends_on` entry is another property's `.ref`, and the registry that assembles a run (below) rejects duplicate names and checks each edge against a registered property's ref by identity, so name and identity together pin a property uniquely and `DepContext.annotation(ref, scope)` can return that property's annotation at the recovered type without a guess. `annotation` returns `None` when the dependency is silent at that scope (the node was not grounded or not reached); a transfer reads `None` as the dependency's lattice top, the same "we don't know" every other absence means. A transfer reading a relation-scoped dependency from a column node derives the relation scope from the column's own `ColumnRef.source`; that is the only legal way to produce the `S2` the read requires.

The channel does not manufacture information. If no one ever typed `amount` as `Money`, there is no currency refinement on that column, and the mixed-currency `SUM` draws no finding. That is the substrate's posture (absence is silence), not a gap the channel could close.

The `depends_on` graph must be acyclic, so no pair of properties needs a joint fixpoint over a product lattice. An authoring lint warns when a core transfer reads a user-declared axis, since that quietly makes a structural conclusion conditional. The user writes none of this: the edge originates in the coherence declaration on the `Money` type, and the framework compiles that intent into the dependency and the transfer.

## What a fact is

A **fact** is a typed claim about one node of the lineage graph, under one property, with provenance. A node is either a column or a relation, and those are the only two subjects a fact can have. Anything that looks multi-column is a relation fact whose *value* names the columns: a candidate key `{customer_id, region}` is the statement "this relation is unique on `{customer_id, region}`," so it attaches to the relation and the column set lives in the value, never in the address.

A fact grounds a node in one of two ways, depending on whether the node has a derivation:

- **Anchoring.** No derivation (a source or seed column, or the source relation itself). The fact is the only input the propagator has.
- **Asserted.** The node is derived (a model output column, a model's candidate key emerging from a `SELECT`). The fact is a developer or contract claim about what the derivation produces. The propagator uses it forward and checks it against the upstream.

Facts must be rock-solid because detectors rely on them silently. A wrong fact produces a wrong annotation produces a false-positive finding; an absent fact produces a missing annotation produces a silent skip. The audit is louder when it knows and quieter when it does not.

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

# A world assignment chosen by the flag layer: the value each enumerated flag takes
# in this run. Opaque to the substrate in meaning, but its identity must be stable:
# facts bucket by world, so a world has to be hashable with value equality. Flag
# values are primitives or enums, hence Hashable. The flag system defines the
# contents; the substrate only relies on equality and hashing.
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
    pure value-domain map with no DepContext: given the aggregate and its input annotation
    it returns the output, and it must commute with confluence and cross so the single-pass
    walk is plan-shape independent. ``coherence`` is the optional clear-on-failure guard,
    and the FD read it performs is the only way a dependency enters an aggregate, so ``core``
    is PBT'd in isolation. A counting or accumulating property supplies a ``core`` that is
    its semimodule action; the commutation obligation is then the semimodule homomorphism
    law inherited from its semiring."""
    core:      Callable[[exp.AggFunc, Annotation[K]], Annotation[K]]
    coherence: CoherenceGuard | None = None


class FactDiscoverer(Protocol[K, S]):
    """Reads the manifest and dblect declarations, returns facts for any node it can
    ground. Pure, and it returns a materialized collection so that a discoverer which
    raises drops all of its facts and none of another's."""

    def discover(
        self, manifest: "Manifest", *, name_to_source: Mapping[str, SourceRef],
    ) -> Collection[Fact[K, S]]: ...


# The existing operator algebra (dblect.lineage.semiring.Semiring), now an optional
# slot rather than a mandatory field: present only for a property whose confluence or
# cross counts or accumulates. When present, the engine derives the relational transfers
# from it (UNION ALL is plus, JOIN is times) and the semiring laws become the obligations
# that buy plan-shape independence. Left unset for the idempotent and value-domain
# properties this module ships; populated by the accumulation properties (where-provenance,
# aggregation depth) and the counting properties on the semiring substrate of
# column-level-lineage.md. Concrete instances are frozen dataclasses (BooleanSemiring,
# UnionSemiring), laws PBT'd already.
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

Two invariants hold the optional `semiring` slot together with the rest. When it is set, the relational operators (`Union`, `Join`) are derived from `plus`/`times` and must not be redefined in `operators`, and an idempotent semiring must satisfy `plus == lattice.join` so confluence has one answer. Both are checked once at construction. `consistent` and `resolve` are derived from `lattice`, so they are not fields. Two smart constructors, `column_property` (fixing `scope_kind=COLUMN`) and `relation_property` (fixing `RELATION`), set `scope_kind` from the scope type so the field cannot drift from `S` in practice; the field stays for runtime walk dispatch, where the erased `S` is not available. A constructor wires a property from its discoverers:

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

**Compile-value facts share one world.** The flag layer fixes one world per propagation run, and the compile-value discoverers emit their facts under it, so every fact in a bucket shares that world and resolution is ordinary. A var-derived value is ground truth in its world, the same standing as a native constraint or a user assertion. A difference *between* worlds is the flag-world analysis ([`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md)), reported as "this contract holds under world A and fails under world B." Where a world-scoped value and an unconditional assertion genuinely disagree on one axis (a contract claims `contains_tax=TRUE` always, world B produces `FALSE`), that disagreement is the finding the analysis exists to raise.

A single propagation under a single world is one evaluation of each property's derivation. The same derivation evaluated under different world assignments is what the flag-world analysis compares, so multi-world cost is driven by re-evaluating derivations under assignments rather than by anything in this module. The audit exposes a "trace this annotation to its grounding facts" helper that reconstructs a derivation on demand.

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

Three things this fixes are load-bearing. **Name uniqueness plus ref identity is enforced here**, which is what makes the typed dependency read sound: a `PropertyRef` is minted only inside `Property` (its constructor rejects a hand-built handle), a name maps to exactly one registered property, and a `depends_on` edge is checked against the minted ref by identity, so `dep_context(...).annotation(ref, scope)` returns the right value at the right type and a forged handle cannot mistype the read. **Ordering is automatic.** A user property declares `depends_on` on a built-in property's `ref` (the coherence edge on `Money` reads the functional-dependency property), and `evaluation_order` interleaves it with the built-ins; the author writes no ordering. **A cycle or a dangling edge is a build error, not a runtime surprise**, so the acyclic `depends_on` guarantee the single-pass walk rests on is checked once at assembly. The propagator runs each property in `evaluation_order`, accumulating annotations into the store the next property's `DepContext` reads.

## Validation and propagation

At a node the propagator has up to two inputs: the **inferred** annotation, from walking the upstream expression (absent at sources and seeds), and the **declared** annotation, from `ground` (top-`IMPLICIT` when nothing is declared, top-`EXPLICIT` for an opt-out, a `REFINED` value where a fact resolved). Two independent decisions follow.

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

The diagnostic is a fixed template, not synthesized prose. dblect cannot author domain narrative like "this mixes tax-inclusive and tax-exclusive amounts," because it does not know what a user-domain axis means. It fills slots from what it has: the site, the operator, the two operand columns and their types, the axis that cleared, and the suppression path. The only domain-flavored text is a name the modeler chose, drawn from the property's `display` slot (the `AxisDisplay` name and optional one-line description), with fallback to the bare type and axis names when no `display` is supplied. The `display` slot is the reserved seam the types layer fills from a declaration; the substrate plumbs the slot and never authors the text. A realistic rendering:

> `orders.sql:12`: `total` combines `revenue` and `net_revenue` with `+`. `net_revenue` is `RevenueWithTax` but `revenue` carries no refinement on `contains_tax`, so the result drops it. Annotate `revenue` as `RevenueWithTax` if it qualifies, or treat this as a possible mismatch. To silence: mark `revenue` opaque, or disable `refinement-erased-at-seam` for this model.

## Soundness contract

1. **Discoverer correctness is a hard guarantee for the input it reads.** A discoverer that emits a fact its declaration does not support is a substrate bug. PBT covers each shipping discoverer. Whether the resulting conclusion is unconditional depends on what it rests on: one built only from core transfers is a theorem given the declared inputs; one that uses a user signature holds given the declared inputs and that signature.
2. **Transfer rules stay safe, and that obligation is explicit.** Every rule is conservative: it never reports more than the SQL guarantees, and a more precise input never yields a less precise output (what the references call sound and monotone). This obligation is over the annotation's `value`, the `K` carried in the lattice; the framework proves it for its own rules, once. The lattice top is the only value a rule may emit without proof: every value strictly below top is a positive claim the rule asserts, so an intermediate value like `NULLABLE` or a concrete interval must be something the SQL established, not a default reached on uncertainty. The conservative default on any uncertainty is always top; emitting an intermediate value by default would, for instance, let an unproven `NULLABLE` false-conflict a declared `NON_NULL` at the consistency check. A user-supplied rule must meet the same bar; that is the obligation the author's vouch discharges, and the runtime layer catches an inaccurate vouch empirically. An aggregate rule's `core` additionally commutes with confluence and cross, by the semimodule law where a semiring is present and by direct discharge otherwise; the obligation is over the `core` alone because it is pure, and the optional `CoherenceGuard` clears to top, which commutes with confluence trivially. A transfer that reads a dependency through `DepContext` carries one more obligation: it is monotone in the dependency's value as well as its own, so a dependency degrading toward top can only make the transfer more conservative, never let it claim a more precise result. A silent dependency reads as top, which is the most conservative input, so this holds for the absent case by construction.
3. **What flows is deterministic, and the two diagnostic bits are advisory.** The propagator carries an `Annotation`, not a bare `K`. Soundness and monotonicity are claims about its `value`. The `opacity` and `provisional` bits are diagnostic metadata, not part of the precision order, and they are deterministic functions of the walk: the lineage graph is a DAG visited in dependency-then-topological order, so each node's full annotation is fixed by its inputs. `provisional` clears on a fresh consistent anchor, which is non-monotone along dataflow on purpose (error recovery), and it is read only to *downgrade* a finding's severity, never to license a more precise value or suppress a sound finding. So the value's soundness is independent of the taint.
4. **Absence is silence.** A node nothing declares grounds as a top-`IMPLICIT` declared annotation, the propagator returns the lattice top, and detectors read it as "we don't know."
5. **Conditional facts are captured but not activated.** A `not_null` or `unique` with a `where` filter produces a fact carrying the predicate, and grounding ignores the predicate for now. A `where` filter is selection, which the provenance tradition handles by conditioning the annotation; activation follows the rule [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md) commits to, so the deferral is engineering sequencing rather than an open question.
6. **Contradictions are resolved and surfaced.** Two declarations whose values meet to `bottom` raise a `BuildIssue`; resolution keeps a deterministic value and never picks a winner from provenance. An inferred value that reaches `bottom` during propagation fails `consistent` and is a finding, never a vacuous pass.
7. **Facts cross model boundaries only through propagation.** The flow value carries downstream through the lineage graph; the boundary value gates cross-model contract checks.
8. **Asserted facts are checked, and the boundary is stable.** A fact on a derived node runs through `consistent` against the inferred value. A mismatch is a finding. The declared value remains the contract callers built against, and downstream-of-violation annotations are provisional.

## Trusting unenforced constraints

Framework transfers are theorems; leaf facts are not. The transfer rules are proven once, but the candidate key, foreign key, or `not_null` claim that seeds the propagation is an assertion the framework cannot verify, since it never reads source data. So even a core conclusion is conditional: given the declared source facts, the propagated values are theorems. "Proven" means proven from the declared inputs, not verified against data. A constraint the warehouse declares but does not enforce is a leaf-fact risk, not a transfer-rule risk: it can make a propagated annotation wrong about the data while the rules that produced it stay sound.

Many warehouses (Snowflake, BigQuery, Redshift, Databricks) treat `PRIMARY KEY`, `UNIQUE`, and `FOREIGN KEY` as informational. Some support a `RELY` form the optimiser trusts for rewrites without validating the data, the same conditional bet this substrate makes; others are documentation only. Whether a native constraint actually backs its claim is an adapter-and-constraint-kind question (Databricks enforces `CHECK` but not `PRIMARY KEY`, Snowflake enforces neither), captured by `enforced_on_write` on the `NativeConstraint` provenance. For dblect's purposes the question collapses to one: is the claim checked against data by something that runs? A dbt `unique` test is, because the runtime layer runs it; an advisory `PRIMARY KEY` is not. This is why provenance carries no authority order: the signal that matters is whether a running guard exists, read where it is needed.

Two things follow:

- **Discoverers are adapter-aware about enforcement.** The native-constraint discoverer knows the active adapter and sets `enforced_on_write` on each fact. This is descriptive provenance; resolution never reads it.
- **The runtime layer is the backstop, and the gap gets a finding.** The audit's empirical checks and the generator intents named for these violations (Orphan, NullKey, Duplicate, Boundary) test whether advisory constraints hold. The finding is scoped to constraints that actually carry weight, not every advisory constraint. A constraint-derived annotation is **load-bearing** when at least one reported conclusion depends on it: a finding it suppressed (the Duplicate detector stayed silent because the key was assumed unique), a boundary check it let pass, or a finding it raised. Operationally, it is load-bearing when dropping the constraint to top would change what the audit reports, which the "trace this annotation to its grounding facts" helper makes computable: the fact appears in the trace of some reported result. Where such an annotation rests on a native constraint with `enforced_on_write=False` that no running test covers at the same scope, the audit emits a finding ("uniqueness on `dim_customer.id` rests on an advisory `PRIMARY KEY` and no `unique` test guards it; add a test"), turning a silent assumption into a recommendation. The suppressed-finding case is the important one, since that is the silent false negative the advisory constraint can hide.

## Coverage and degradation

Silent degradation is sound but it can hide behind itself: a manifest where sqlglot resolves few columns produces few annotations and few findings, which can read as a clean bill rather than as thin coverage. The audit treats coverage as a first-class output, and keeps two metrics separate because they mean opposite things.

- **Resolution coverage** is the fraction of columns whose lineage the propagator could follow against the fraction it fell blind on (sqlglot could not resolve the column, a macro escaped rendering, a dialect construct misparsed). Blindness is a capability gap, so a configurable floor turns sustained blindness into a finding ("resolved 38% of columns on `fct_orders`; analysis below covers only what was resolved"). The floor keys on resolution only.
- **Grounding coverage** is, among resolved columns, how many a fact grounded, reported per discoverer. An ungrounded column is the expected case under "absence is silence," not a defect, so grounding coverage never trips a floor on its own. Where it earns a finding is scoped to declared intent: of the columns a contract names, how many resolved to a checkable annotation. That number tells a partial adopter whether their declarations are actually being checked, and it does not fire in the zero-declaration layer where ungroundedness is the whole point.

Separating the two keeps the floor from reporting thin coverage in exactly the adoption mode the design courts, where most columns legitimately carry no fact. The default posture stays silent-on-blindness for individual nodes; the floor is about the aggregate.

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

**Encoding.** Uniqueness becomes a `Property[CandidateKeySet, SourceRef]`, built entirely from core transfers. The K-relations encoding from [`column-level-lineage.md`](./column-level-lineage.md) (`K = frozenset[frozenset[ColumnRef]]`) supplies the algebra. The candidate key is the *value* at the relation node, never a column-set address. The value carries an invariant: it is an antichain under set inclusion, no key a superset of another, so it holds *candidate* (minimal) keys rather than every superkey. Every transfer and `resolve`'s meet re-minimize, dropping any key that subsumes a smaller one. Confluence keeps the keys both branches carry; a `JOIN` combines keys subject to join-condition coverage; `DISTINCT` and top-level `GROUP BY` introduce the projection set as a key. `UNION` (distinct) makes the whole projected row a *superkey*, contributed only where no existing smaller key already subsumes it, so the antichain invariant is what keeps a non-minimal row-key from polluting the set and weakening downstream `JOIN` results.

**Discoverers.** All produce relation facts whose value is a key set:

| Manifest input                                 | Fact                                                            |
|------------------------------------------------|-----------------------------------------------------------------|
| `unique` test on column `c`                    | `Fact(model, {{c}}, Declared(DBT_GENERIC_TEST))`                |
| `unique_combination_of_columns(c1, c2, …)`     | `Fact(model, {{c1, c2, …}}, Declared(DBT_UTILS_TEST))`          |
| Native `PRIMARY KEY (c1, c2)`                  | `Fact(model, {{c1, c2}}, NativeConstraint(enforced_on_write=…))`|
| Native column-level `UNIQUE` on `c`            | `Fact(model, {{c}}, NativeConstraint(enforced_on_write=…))`     |

Resolution is the lattice meet, which for uniqueness unions independent declared keys, so no contradiction arises. Provenance records which declaration each key came from, for the trace.

**What requires care.** The relation-algebra walk is new substrate. The K-relations literature is most natural at the row level, and lifting to per-node annotations means a transfer rule has to be explicit about whether it reads the upstream relation's annotation or the upstream columns' annotations. The operator rules in `column-level-lineage.md` get this right for uniqueness; new relation-scoped properties should reuse the pattern. Conditional uniqueness facts carry over with the same deferral as [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md): the substrate captures the predicate, activation lands when a concrete consumer asks.

**Plan-independence is a key-propagation result, not a semiring one.** Uniqueness is key and functional-dependency propagation (Abiteboul, Hull, Vianu) rather than a provenance-semiring property, which is why it carries no `semiring` slot. Its `JOIN` combine reads the equijoin predicate, so it is not a binary operation on `K` and not a semiring `×`; the intended technique is to normalize the equijoin into a column-equivalence and combine the quotiented key sets, with theta and non-equijoins re-anchoring opaque the same way recursive CTEs do. Its `UNION DISTINCT` confluence is the domain join plus a key-introduction step (the deduped row is a superkey), so uniqueness confluence is keyed on the operator rather than being a single `lattice.join`. The plan-independence theorem for this fragment (annotations invariant under join reordering and associativity over the equijoin SPJ core) lands with this migration in `column-level-lineage.md`; this section is the pointer, not the proof.

**Sequencing.** The migration is its own change after the substrate lands with nullability. The existing uniqueness path keeps backing the detectors while the new path is built, a "both paths agree on jaffle" test pins parity for the cut-over, and after cut-over the old path retires. This closes [`#16`](https://github.com/dvryaboy/dblect/issues/16): facts on a `JOIN`'s upstream propagate through the cross rule to the output, so the multi-source special case stops being special.

## What this does not cover

- **Activation of conditional facts.** See [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md).
- **World enumeration over flag values.** Belongs to the flag system. This module supplies values inside a world; the flag layer chooses worlds and compares evaluations across them.
- **Cross-package fact inference.** Facts declared in a dbt package and consumed by a downstream package that does not import it. Same scope cut as [`var-inference-spec.md`](./var-inference-spec.md).
- **Runtime facts from the warehouse.** `INFORMATION_SCHEMA` or adapter-side metadata. Lands when an adapter-aware fact source is requested.
- **Inference from SQL.** A column projected as `COALESCE(x, 0)` grounds a nullability annotation through the property's operator rules, not through a fact. Facts are declarations; inference is the propagator's job.
- **Recursive-CTE propagation.** Treated as an opaque boundary that re-anchors on output, because a recursive CTE needs a fixpoint the single-pass walk does not run.
- **Window propagation.** This substrate re-anchors at window outputs for now, but the cut is narrower than it looks and a later phase takes it on. A window is row-preserving, so cardinality, existing keys, the `ROW_NUMBER` key-introduction, filter constant-elimination, and currency-coherence can propagate through it soundly, with only the ordering-determinism refinement genuinely erasing. The design is in [`window-propagation.md`](./window-propagation.md).

## Sequencing

1. The data model (`Fact`, `Provenance`, `Annotation` with its unified `Opacity`, `Lattice`, `Semiring`, `AggregateRule` and `CoherenceGuard`, `PropertyRef`, `DepContext`, `FactDiscoverer`, `OpaqueReader`, `collect`, `grounding`, the `BuildIssue`/`SeamContradiction`/`DiscovererError` errors), the `Property` shape (`scope_kind`, `lattice`, `ground`, optional `semiring`, optional `display`, `depends_on`) with its `column_property`/`relation_property` constructors, and the `PropertyRegistry` that orders properties and enforces name uniqueness. The propagator grounds at every node, runs `consistent` when both inferred and declared are present, carries `Annotation` through transfers, and dispatches its walk on `scope_kind`. Ships with nullability, which leaves `semiring` and `display` unset.
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
- **Transfer obligations.** Monotonicity of each shipped transfer over its annotation `value`; commutation of each aggregate rule's `core` with confluence and cross. The `core` is pure, so this is checked without constructing a `DepContext`; a `CoherenceGuard` is tested separately for clear-on-failure, and a transfer reading a dependency is tested monotone in that dependency's value too (a dependency degrading toward top must not let the transfer claim more). A user-domain property's transfers are tested for monotonicity in the same harness so a registered axis cannot break the property quietly.
- **Semiring laws (when a `semiring` is present).** A property carrying a `semiring` is PBT'd for associativity, commutativity, distributivity of `times` over `plus`, the identity and annihilation roles of `zero`/`one`, and, for an idempotent semiring, `plus == lattice.join`. This is the obligation that buys plan-shape independence for the counting and accumulating properties; the properties shipped here carry no `semiring` and skip it.
- **Annotation determinism.** The full annotation at each node is a deterministic function of its inputs under the dependency-then-topological walk: same graph and same facts yield the same `opacity` and `provisional`, and a `provisional` taint only downgrades finding severity, never licenses a more precise value.
- **Dependency-read soundness.** A registry with a duplicate property name, a `depends_on` cycle, or an edge to an unregistered property fails assembly; a `DepContext` read returns the dependency's annotation at the recovered type, and a silent dependency reads as top.
- **Opaque grounding.** A scope in the opaque-opt-out set grounds as a top-`EXPLICIT` declared annotation rather than `REFINED` or `IMPLICIT`, regardless of any facts also present, and flows silently.
- **Seam diagnostic.** An `EXPLICIT` top meeting a declared refinement is silent; an `IMPLICIT` top meeting one is silent at the zero-declaration layer and a finding at the typed layer; two committed incompatible operands are a finding at both. The diagnostic names the column, both readings, and the suppression path.
- **Resolution determinism.** A bucket of facts in any order resolves to the same value; a `bottom` contradiction raises a `BuildIssue` and yields the same deterministic value regardless of order. Compile-value facts sharing one `WorldRef` bucket by world equality, so resolution within a world is order-independent.
- **Tightening and boundary.** A structural property whose inferred value is strictly more precise than the declaration propagates the inferred value as flow, keeps the declared value as boundary, and emits the soft finding. A user-domain property does the same through a preserving chain, and a clearing step stops the tightening.
- **Asserted-fact end-to-end.** A `not_null` declaration on a column with a `NULLABLE` upstream surfaces a finding and propagates the declared value downstream as provisional; the same with a `NON_NULL` or top upstream propagates without a finding. The analogous test for a candidate-key declaration on a derived model.
- **Coverage reporting.** A deliberately under-resolvable model reports low resolution coverage and trips the floor finding; a fully resolvable model with no declarations reports full resolution coverage and low grounding coverage and trips no floor, so absence-is-silence does not read as thin coverage.
- **Uniqueness parity.** Before retiring the old uniqueness path, run both against the jaffle fixture and assert agreement on every model's candidate keys.

## Companion docs to update on adoption

Adopting this evolves `Property` and the propagator, so a few companion docs gain the new shape when the implementation lands:

- [`column-level-lineage.md`](./column-level-lineage.md): `Property` gains `scope_kind`, `lattice`, `ground`, an optional `semiring`, an optional `display`, and `depends_on`; the bare `Semiring`-keyed bundle becomes the optional operator-algebra slot the accumulation properties (where-provenance, aggregation depth) and the counting properties (cardinality) populate while the idempotent and value-domain properties leave it unset; transfers take a read-only `DepContext` and carry `Annotation`; properties are assembled through a `PropertyRegistry` that fixes evaluation order and enforces name uniqueness; the propagator evaluates properties in dependency order, dispatches its walk on `scope_kind`, and grows the relation-algebra path.
- [`design-concepts-digest.md`](./design-concepts-digest.md): the structural/user-domain split is expressed as where a property's transfers come from (the proven core or a user declaration), with the composition rules organised by relational operator into forced-versus-chosen and the aggregate behaviour named *combinability*.
- [`conditional-uniqueness-facts.md`](./conditional-uniqueness-facts.md): the model-keyed conditional fact becomes a relation-scoped `Fact` carrying the predicate.

### Existing lineage code this revises

The substrate already exists in skeletal form; adopting this design evolves it rather than starting fresh. The semiring interface survives; what changes is that it stops being mandatory.

- [`semiring.py`](../../src/dblect/lineage/semiring.py): the `Semiring` protocol and its `BooleanSemiring` / `UnionSemiring` instances are kept as the optional operator-algebra slot. Their laws are already PBT'd, so the obligation that buys plan-shape independence for the accumulation and counting properties is in place. No reshape, only a change of role from required field to optional slot.
- [`property.py`](../../src/dblect/lineage/property.py): the substantive rewrite. `Property[K]` becomes `Property[K, S]` and gains `lattice`, `scope_kind`, `ground` (replacing `source`), the optional `semiring`, `display`, and `depends_on`. The propagator threads a `DepContext` and carries `Annotation` instead of bare `K`. Confluence stops hardwiring `semiring.plus`: it uses the property's confluence combine, which is `lattice.join` when no semiring is present and `semiring.plus` when one is. The `times`-fold fallback applies only when a semiring is present; otherwise the property supplies the rule or the result is `lattice.top`. `default()` returns `lattice.top` (today's `unknown_value`). The single-`ColumnRef` per-column walk stays for column-scoped properties; the relation-algebra walk for `SourceRef`-scoped properties is the new path the uniqueness migration adds.
- [`properties/nullability.py`](../../src/dblect/lineage/properties/nullability.py): the demo becomes the production property of step 2. `NullabilitySemiring` (where `plus == times`, both "any nullable taints") is replaced by a `NULLABILITY_LATTICE` whose join is exactly that taint rule, with `semiring=None`; `_source_unknown` becomes `ground` over the nullability discoverers.
- [`properties/where_provenance.py`](../../src/dblect/lineage/properties/where_provenance.py) and [`properties/aggregation_depth.py`](../../src/dblect/lineage/properties/aggregation_depth.py): the in-tree examples of a populated `semiring` slot, so the optional slot is exercised rather than hypothetical. They keep their semirings and gain a `lattice` for resolution and consistency.
- `tests/lineage/test_semiring_laws.py`: keeps the semiring-law PBT, now scoped to the properties that carry a semiring, and gains the lattice-law PBT that every property runs.

## References

- The substrate this layers on: [`column-level-lineage.md`](./column-level-lineage.md), including the K-relations encoding for uniqueness.
- The structural and user-domain transfer vocabulary: [`design-concepts-digest.md`](./design-concepts-digest.md).
- The end-user declaration surface the facts layer carries: [`dblect_technical_intro.md`](./dblect_technical_intro.md) and [`flags_and_configs_as_types.md`](./flags_and_configs_as_types.md).
- The current uniqueness facts module: [`../../src/dblect/uniqueness/facts.py`](../../src/dblect/uniqueness/facts.py).
- Foundational literature. Abstract interpretation (Cousot and Cousot) is the framework this engine is an instance of, and the source of the monotone-transfer and sound-over-approximation obligations. Provenance semirings (Green, Karvounarakis, Tannen 2007) and functional-dependency propagation (Abiteboul, Hull, Vianu) supply the algebra's shape for the counting and accumulating properties; aggregate provenance (Amsterdamer, Deutch, Tannen 2011) is why aggregation gets its own transfer slot with a commutation obligation rather than riding the bare lattice. The why-provenance and hypothetical-query line (Karvounarakis and collaborators) is the model for evaluating one derivation under different world assignments. The type-qualifier tradition (CQual, FlowCaml) is the closest analogue for the user-domain lattice, and the gradual-typing tradition (Siek and Taha; Wadler and Findler on blame) for the typed/untyped seam. SQL formal semantics (HoTTSQL, Cosette) underpins the operator rules; Pandera and Pydantic shape the declaration surface.
- Issue [`#26`](https://github.com/dvryaboy/dblect/issues/26): promotes the demo nullability and aggregation-depth properties. Issue [`#16`](https://github.com/dvryaboy/dblect/issues/16): multi-source uniqueness detectors consume the substrate.
