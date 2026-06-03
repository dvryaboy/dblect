"""Value types for the facts substrate: facts, provenance, and annotations.

A :class:`Fact` is a typed, provenance-carrying claim about one node of the
lineage graph (a column when ``S`` is :class:`ColumnRef`, a relation when ``S``
is :class:`SourceRef`), under one property. Provenance records where the claim
was authored and carries no authority ordering: conflicts between facts at the
same node are resolved by the property's lattice, never by ranking channels.

An :class:`Annotation` is what the propagator stores and passes. It is a value
plus two diagnostic bits: an :class:`Opacity` tag that answers "is a top value
chosen or incidental?", and a ``provisional`` error-recovery taint. Neither bit
is part of the precision order; see ``propagation-soundness.md``.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from dblect.lineage.graph import ColumnRef, SourceRef

K = TypeVar("K")
# A property is column- OR relation-scoped, never both; the bound keeps a column
# property from being handed a relation fact.
S = TypeVar("S", ColumnRef, SourceRef)


@dataclass(frozen=True, slots=True)
class WorldRef:
    """A world assignment chosen by the flag layer. Opaque to the substrate in
    meaning; facts bucket by world, so its identity must be stable (hashable,
    value equality)."""

    assignments: frozenset[tuple[str, Hashable]]


class ScopeKind(StrEnum):
    COLUMN = "column"  # propagator walks per-column projections
    RELATION = "relation"  # propagator walks relation-algebra structure


class DeclaredSource(StrEnum):
    DBT_GENERIC_TEST = "dbt_generic_test"  # not_null, unique, accepted_values, ...
    DBT_UTILS_TEST = "dbt_utils_test"  # unique_combination_of_columns, accepted_range, ...
    COLUMN_METADATA = "column_metadata"  # data_type, nullable in schema.yml
    DBT_META = "dbt_meta"  # meta.dblect.* blocks in schema.yml
    MODEL_CONTRACT = "model_contract"  # dbt model-contract declaration
    USER_ASSERTED = "user_asserted"  # Python SemanticType / Field / ModelContract


class CompileOrigin(StrEnum):
    DBT_VAR = "dbt_var"  # var() from dbt_project.yml; statically enumerable
    ENV_VAR = "env_var"  # env_var(); statically enumerable
    DBT_CONFIG = "dbt_config"  # node.config[...] key
    COMPUTED = "computed"  # Jinja/Python substitution, possibly a DB call; opaque to enumeration


@dataclass(frozen=True, slots=True)
class Declared:
    """Authored directly: a dbt test, schema.yml metadata or meta, or a Python contract."""

    source: DeclaredSource


@dataclass(frozen=True, slots=True)
class NativeConstraint:
    """A warehouse or dbt 1.5+ constraint. ``enforced_on_write`` exists only here
    and records whether the active adapter enforces the constraint on write. It is
    read by the unenforced-constraint finding, never by fact resolution."""

    enforced_on_write: bool


@dataclass(frozen=True, slots=True)
class CompileValue:
    """A value resolved at compile time. ``world`` exists only here and is never
    absent: a compile value is ground truth in exactly the world the flag layer
    fixed for this run. ``origin`` decides whether the flag layer can enumerate
    worlds over it."""

    origin: CompileOrigin
    world: WorldRef


# Each variant carries exactly the fields valid for it; a field meaningful for
# one kind of fact does not exist on another.
Provenance = Declared | NativeConstraint | CompileValue


@dataclass(frozen=True, slots=True)
class Fact(Generic[K, S]):
    """One claim about one node, under one property.

    A candidate key ``{customer_id, region}`` is a relation fact whose *value*
    names the columns: the address is the relation, never the column set.
    """

    scope: S
    value: K
    provenance: Provenance
    detail: str | None = None


class Opacity(StrEnum):
    CONCRETE = "concrete"  # value carries information (value is not top)
    EXPLICIT = "explicit"  # value is top by a declared opt-out; flows silently
    IMPLICIT = "implicit"  # value is top incidentally (nothing declared it); warns at a seam


@dataclass(frozen=True, slots=True)
class Annotation(Generic[K]):
    """A propagated value plus its diagnostic bits.

    ``opacity`` carries information only when ``value`` is the lattice top:
    ``CONCRETE`` *is* "value is not top", and the choice that matters is
    ``EXPLICIT`` (a declared opt-out, flows silently) versus ``IMPLICIT``
    (incidental top, warns at a refinement seam).

    ``provisional`` is the one bit that is not about knowing or not knowing: an
    error-recovery taint set when a node's inferred value conflicts with its
    grounded value, propagated as the logical OR of a transfer's inputs, and cleared
    when a node is freshly anchored by a consistent fact. It never licenses a more
    precise value; detectors may downgrade a finding that rests on it.
    """

    value: K
    opacity: Opacity = Opacity.CONCRETE
    provisional: bool = False
