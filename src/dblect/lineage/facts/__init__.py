"""The facts substrate: declarations become grounded values that enter the
property walk, under one soundness contract.

The design is in ``docs/design/lineage-facts.md`` and the full type surface in
``docs/design/lineage-facts-types.md``. A :class:`Fact` is a typed,
provenance-carrying claim about one column or one relation; a property's
:class:`Lattice` resolves several facts at a node into the :class:`Annotation`
the propagator reads at leaves and checks at derived nodes.
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
