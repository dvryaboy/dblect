# Lineage facts: type reference

Status: reference. The design and its rationale are in [`lineage-facts.md`](./lineage-facts.md); this is the complete type surface the facts module exposes, collected for implementers. Names and shapes here are the contract the design narrative refers to.

## Imports, scopes, worlds

```python
from typing import Any, Callable, Collection, Generic, Hashable, Mapping, Protocol, TypeVar, final, runtime_checkable
from dataclasses import dataclass, field
from enum import StrEnum

from dblect.lineage.graph import ColumnRef, SourceRef
from sqlglot import Expr                        # the sqlglot expression base type
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

## Facts and provenance

Provenance records where a fact was authored, for tracing and reporting. It carries no authority ordering: conflicts are resolved by the lattice, never by ranking channels. Each variant carries exactly the fields valid for it.

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

## Annotations

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

## Grounding

An opaque opt-out is not a fact (a discoverer never emits a top-valued fact), so the grounding builder synthesizes it as a top-`EXPLICIT` annotation from an `OpaqueReader`, consulted before facts.

```python
class OpaqueReader(Protocol[S]):
    def opaque_scopes(self, manifest: "Manifest", *, name_to_source: Mapping[str, SourceRef]) -> Collection[S]: ...
```

## The lattice

A property states its order once, as a `Lattice`. Resolution and the validation check both derive from it, so they cannot drift apart.

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
    mutually unsatisfiable; the caller raises a FactConflictError and keeps this deterministic
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

## Properties and transfers

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
    forged handle fails assembly rather than silently mistyping a read."""
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
    clears to top and the seam rule reports it. Compiles from a ``within=<cols>`` declaration."""
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
    """The human-facing names the seam diagnostic fills its template from. The types layer
    supplies it from a declaration, with fallback to the bare type and axis names."""
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

The optional `semiring` slot carries one construction-time check: when it is set, the relational operators (`Union`, `Join`) are derived from `plus`/`times` and must not be redefined in `operators`. Its algebraic laws (associativity, commutativity, distributivity, the identity roles) are semiring-law property tests (see [`propagation-soundness.md`](./propagation-soundness.md)) rather than construction-time checks, since function equality is not decidable by inspection. The `plus` need not equal the lattice join: nullability is idempotent yet its confluence lets a committed value beat the "no information" top, which a join with the top cannot. `consistent` and `resolve` are derived from `lattice`, so they are not fields. Two smart constructors, `column_property` (fixing `scope_kind=COLUMN`) and `relation_property` (fixing `RELATION`), set `scope_kind` from the scope type.

## Collection, grounding, errors

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
) -> Callable[[S], Annotation[K]]:
    """Fold each scope's bucket through ``resolve``, raise a ``FactConflictError`` on a ``bottom``
    contradiction, and return the declared annotation: ``Annotation(top, EXPLICIT)`` for a
    scope in the opaque set, ``Annotation(value, REFINED)`` where a value resolved, and
    ``Annotation(top, IMPLICIT)`` otherwise."""
```

A constructor wires a property from its discoverers; nullability is the worked shape:

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

The errors are a small sealed set. `FactConflictError` is raised by resolution when a scope's facts meet to `bottom`; it carries the scope and the conflicting facts, is collected and reported rather than aborting, and the run continues from the deterministic `bottom`-derived value with downstream annotations marked provisional. `SeamContradictionError` is raised by `combine` when two committed operands are incompatible; it becomes a finding at the combine site. `DiscovererError` is the only exception `collect` treats as expected, isolating one discoverer's failure from the rest.

## The annotation store

The propagator runs each property in `evaluation_order` and writes every node's annotation here as it goes, so a later property's `DepContext` can read an earlier one's results. The store is mutable for the duration of one run and never shared across runs; `DepContext` is the read-only projection of it the registry hands to transfers.

```python
@dataclass(slots=True)
class AnnotationStore:
    """Annotations accumulated across properties during one propagation run, keyed by
    property name and scope. A scope is a ColumnRef or a SourceRef, so a single store
    holds both column- and relation-scoped properties; the (name, scope) key keeps them
    separate without the store knowing a property's scope kind. Write-once per
    (name, scope) in a correct run: the propagator visits each node once per property."""
    _by_property: dict[str, dict[ColumnRef | SourceRef, Annotation[Any]]] = field(default_factory=dict)

    def record(self, name: str, scope: ColumnRef | SourceRef, annotation: Annotation[Any]) -> None:
        self._by_property.setdefault(name, {})[scope] = annotation

    def get(self, name: str, scope: ColumnRef | SourceRef) -> Annotation[Any] | None:
        return self._by_property.get(name, {}).get(scope)
```

`DepContext.annotation(ref, scope)` resolves through this store: the registry recovers the property name from `ref` (identity-checked at assembly), reads `store.get(name, scope)`, and returns it at the dependency's value type `K2`. A `None` return is the silent-dependency case every transfer reads as that dependency's lattice top. The recovery of `K2` is sound because `ref` is the minted `PropertyRef[K2, S2]` whose `K2`/`S2` are the registered property's real types; the store erases to `Annotation[Any]` only internally.

## The registry

```python
@dataclass(frozen=True, slots=True)
class PropertyRegistry:
    properties: tuple[Property[Any, Any], ...]

    def evaluation_order(self) -> tuple[Property[Any, Any], ...]:
        """Topological order over depends_on. Raises on a cycle, on a duplicate name, or
        on an edge whose ref is not the minted ref of a registered property (an identity
        check, not a name match, so a forged handle fails here)."""

    def dep_context(self, store: AnnotationStore) -> DepContext:
        """A read-only view of the annotations computed so far, keyed by (name, scope)."""
```

## The seam combine

The binary combine at a scalar expression decides whether a cleared refinement speaks, from the operands' `opacity`.

```python
def combine(lat: Lattice[K], a: Annotation[K], b: Annotation[K]) -> Annotation[K]:
    m = lat.meet(a.value, b.value)
    if m == lat.bottom:
        raise SeamContradictionError(a, b)                       # two committed, incompatible operands
    if a.value == b.value == m:
        return Annotation(m, provisional=a.provisional or b.provisional)   # agree: preserve
    cleared = a if a.value == lat.top else b                # one committed, the other top: clears
    return Annotation(lat.top, opacity=cleared.opacity,
                      provisional=a.provisional or b.provisional)
```
