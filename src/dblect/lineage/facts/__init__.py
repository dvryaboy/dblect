"""The facts substrate: declarations become grounded values that enter the
property walk, under one soundness contract.

The design is in ``docs/design/lineage-facts.md`` and the full type surface in
``docs/design/lineage-facts-types.md``. A :class:`Fact` is a typed,
provenance-carrying claim about one column or one relation; a property's
:class:`Lattice` resolves several facts at a node into the :class:`Annotation`
the propagator reads at leaves and checks at derived nodes.
"""

from dblect.lineage.facts.grounding import (
    BuildIssue,
    DiscovererError,
    OpaqueReader,
    SeamContradiction,
    collect,
    combine,
    grounding,
)
from dblect.lineage.facts.lattice import Lattice, consistent, resolve
from dblect.lineage.facts.model import (
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
    CoherenceGuard,
    DepContext,
    FactDiscoverer,
    OperatorTransfer,
    Property,
    PropertyRef,
    column_property,
    relation_property,
)

__all__ = [
    "AggregateRule",
    "Annotation",
    "AxisDisplay",
    "BuildIssue",
    "CoherenceGuard",
    "CompileOrigin",
    "CompileValue",
    "Declared",
    "DeclaredSource",
    "DepContext",
    "DiscovererError",
    "Fact",
    "FactDiscoverer",
    "Lattice",
    "NativeConstraint",
    "OpaqueReader",
    "OperatorTransfer",
    "Opacity",
    "Property",
    "PropertyRef",
    "Provenance",
    "ScopeKind",
    "SeamContradiction",
    "WorldRef",
    "collect",
    "column_property",
    "combine",
    "consistent",
    "grounding",
    "relation_property",
    "resolve",
]
