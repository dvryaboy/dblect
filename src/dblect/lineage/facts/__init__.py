"""Turns what a user declared, a dbt test, a schema.yml contract, a Python
``SemanticType``, into the values dblect's propagator checks the SQL against.

A :class:`Fact` is one such declaration, addressed to a column or a relation
and tagged with where it came from. A property's :class:`Lattice` resolves
every fact at a node down to one :class:`Annotation`, which the propagator
reads at the leaves of the lineage graph and checks again at every node
derived from them. See ``docs/design/lineage-facts.md`` (and
``docs/design/lineage-facts-types.md`` for the full type surface) for the
theory.
"""

from dblect.lineage.facts.grounding import (
    DiscovererError,
    FactConflictError,
    OpaqueReader,
    SeamContradictionError,
    collect,
    combine,
    grounding,
)
from dblect.lineage.facts.lattice import Lattice, consistent, resolve
from dblect.lineage.facts.model import (
    BASE_WORLD,
    Annotation,
    CompileOrigin,
    CompileValue,
    Declared,
    DeclaredSource,
    Fact,
    NativeConstraint,
    Opacity,
    Provenance,
    ScopeKind,
    WorldRef,
)
from dblect.lineage.facts.property import (
    AggregateRule,
    AxisDisplay,
    CoherenceClear,
    CoherenceGuard,
    CoherenceSink,
    DepContext,
    DischargePath,
    FactDiscoverer,
    OperatorTransfer,
    Property,
    PropertyRef,
    UndischargedCompanion,
    column_property,
    relation_property,
)
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry

__all__ = [
    "BASE_WORLD",
    "AggregateRule",
    "Annotation",
    "AnnotationStore",
    "AxisDisplay",
    "CoherenceClear",
    "CoherenceGuard",
    "CoherenceSink",
    "CompileOrigin",
    "CompileValue",
    "Declared",
    "DeclaredSource",
    "DepContext",
    "DischargePath",
    "DiscovererError",
    "Fact",
    "FactConflictError",
    "FactDiscoverer",
    "Lattice",
    "NativeConstraint",
    "Opacity",
    "OpaqueReader",
    "OperatorTransfer",
    "Property",
    "PropertyRef",
    "PropertyRegistry",
    "Provenance",
    "ScopeKind",
    "SeamContradictionError",
    "UndischargedCompanion",
    "WorldRef",
    "collect",
    "column_property",
    "combine",
    "consistent",
    "grounding",
    "relation_property",
    "resolve",
]
