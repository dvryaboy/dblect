"""Nullability property: per-column tri-state {NON_NULL, NULLABLE, UNKNOWN}.

The lattice orders by precision (NON_NULL refines NULLABLE refines UNKNOWN, the
"no information" top); ``meet`` keeps the stronger guarantee. A structural
property never contradicts, so the bottom (CONTRADICTION) is unreachable and
exists only to make the lattice bounded.

Confluence uses a semiring rather than the lattice join, so a proven NULLABLE
arm can beat an UNKNOWN one (a join with the top cannot); see
:class:`NullabilitySemiring` and ``propagation-soundness.md``.

Grounding comes from two discoverers that read a dbt manifest: a ``not_null``
generic test and a native ``NOT NULL`` constraint each ground a column to
NON_NULL. Both are sound-by-omission: a disabled test, a ``where``-conditional
test, or an axis they do not own grounds nothing rather than over-claiming. Build
the manifest-backed property with :func:`nullability_property`. The axis pieces a
custom grounding reuses (the lattice, the transfer catalogs, the semiring) are
public, so a graph-only test or a transfer demo can assemble its own property
without a manifest.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlglot import Expr
from sqlglot import expressions as exp

from dblect.lineage.facts.grounding import collect, grounding
from dblect.lineage.facts.lattice import Lattice
from dblect.lineage.facts.model import (
    Annotation,
    Declared,
    DeclaredSource,
    Fact,
    NativeConstraint,
    Opacity,
)
from dblect.lineage.facts.property import (
    AggregateRule,
    DepContext,
    FactDiscoverer,
    OperatorTransfer,
    Property,
    column_property,
)
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.manifest import ConstraintType, Manifest, ResourceType, generic_test_target_uid


class Nullability(StrEnum):
    CONTRADICTION = "contradiction"  # formal lattice bottom; unreachable in propagation
    NON_NULL = "non_null"
    NULLABLE = "nullable"
    UNKNOWN = "unknown"


# Precision rank: smaller is more precise. CONTRADICTION < NON_NULL < NULLABLE < UNKNOWN.
_RANK: dict[Nullability, int] = {
    Nullability.CONTRADICTION: 0,
    Nullability.NON_NULL: 1,
    Nullability.NULLABLE: 2,
    Nullability.UNKNOWN: 3,
}


def _meet(a: Nullability, b: Nullability) -> Nullability:
    return a if _RANK[a] <= _RANK[b] else b


def _join(a: Nullability, b: Nullability) -> Nullability:
    return a if _RANK[a] >= _RANK[b] else b


NULLABILITY_LATTICE: Lattice[Nullability] = Lattice(
    meet=_meet,
    join=_join,
    top=Nullability.UNKNOWN,
    bottom=Nullability.CONTRADICTION,
)


@dataclass(frozen=True, slots=True)
class NullabilitySemiring:
    """The null-taint combine: ``plus`` (confluence) and ``times`` (scalar inputs)
    both take the more-null value, ordering NON_NULL < UNKNOWN < NULLABLE with
    NON_NULL as the identity. A proven NULLABLE taints the result whatever else is
    unknown, and UNKNOWN beats NON_NULL since we never claim non-null without
    evidence. CONTRADICTION never reaches the combine, so the laws are pinned over
    the three operational values in ``test_semiring_laws``."""

    zero: Nullability = Nullability.NON_NULL
    one: Nullability = Nullability.NON_NULL

    def plus(self, a: Nullability, b: Nullability) -> Nullability:
        if a is Nullability.NULLABLE or b is Nullability.NULLABLE:
            return Nullability.NULLABLE
        if a is Nullability.UNKNOWN or b is Nullability.UNKNOWN:
            return Nullability.UNKNOWN
        return Nullability.NON_NULL

    def times(self, a: Nullability, b: Nullability) -> Nullability:
        return self.plus(a, b)


def _coalesce_rule(
    _expr: Expr, kids: tuple[Annotation[Nullability], ...], _ctx: DepContext
) -> Annotation[Nullability]:
    """``COALESCE`` is non-null as soon as one argument is, whatever the rest are."""
    provisional = any(k.provisional for k in kids)
    values = [k.value for k in kids]
    if not values:
        return Annotation(Nullability.UNKNOWN, Opacity.IMPLICIT, provisional=provisional)
    if any(v is Nullability.NON_NULL for v in values):
        return Annotation(Nullability.NON_NULL, provisional=provisional)
    if all(v is Nullability.NULLABLE for v in values):
        return Annotation(Nullability.NULLABLE, provisional=provisional)
    return Annotation(Nullability.UNKNOWN, Opacity.IMPLICIT, provisional=provisional)


def _is_not_null_rule(
    _expr: Expr, kids: tuple[Annotation[Nullability], ...], _ctx: DepContext
) -> Annotation[Nullability]:
    """``x IS NOT NULL`` is a boolean that is itself never null."""
    provisional = any(k.provisional for k in kids)
    return Annotation(Nullability.NON_NULL, provisional=provisional)


def _count_core(_expr: exp.AggFunc, child: Annotation[Nullability]) -> Annotation[Nullability]:
    """COUNT returns 0 for empty groups, never NULL."""
    return Annotation(Nullability.NON_NULL, provisional=child.provisional)


# The transfer catalogs are the reusable axis surface: :func:`nullability_property`
# and any custom-grounding caller (graph-only tests, transfer demos) build their
# property from these plus a ``ground`` function of their own.
NULLABILITY_OPERATORS: Mapping[type[Expr], OperatorTransfer[Nullability]] = {
    exp.Coalesce: _coalesce_rule,
    exp.Is: _is_not_null_rule,
}
NULLABILITY_AGGREGATES: Mapping[type[exp.AggFunc], AggregateRule[Nullability]] = {
    exp.Count: AggregateRule(core=_count_core),
}


# --- discoverers -------------------------------------------------------------

_SOURCE_KIND: Mapping[ResourceType, SourceKind] = {
    ResourceType.MODEL: SourceKind.MODEL,
    ResourceType.SOURCE: SourceKind.SOURCE,
    ResourceType.SEED: SourceKind.SEED,
    ResourceType.SNAPSHOT: SourceKind.SNAPSHOT,
}


def _column_ref(manifest: Manifest, target_uid: str, column: str) -> ColumnRef | None:
    """The graph-keyed ColumnRef for ``column`` on the target node, or None if the
    node is absent or not a data-flow relation. Column names are case-folded to
    match how the builder keys the graph."""
    node = manifest.nodes.get(target_uid)
    if node is None:
        return None
    kind = _SOURCE_KIND.get(node.resource_type)
    if kind is None:
        return None
    return ColumnRef(SourceRef(kind, target_uid), column.lower())


class _NotNullTestDiscoverer:
    """Grounds NON_NULL from enabled, unconditional ``not_null`` generic tests."""

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[Nullability, ColumnRef]]:
        out: list[Fact[Nullability, ColumnRef]] = []
        for node in manifest.nodes.values():
            tm = node.test_metadata
            if tm is None or not tm.enabled or tm.name != "not_null":
                continue
            # A `where` filter makes the assertion conditional ("not null within
            # rows matching X"). Grounding it as an unconditional NON_NULL would
            # over-claim, so it grounds nothing until conditional facts land (see
            # conditional-uniqueness-facts.md).
            if tm.where is not None:
                continue
            col = tm.kwargs.get("column_name")
            if not isinstance(col, str) or not col:
                continue
            target = generic_test_target_uid(node)
            if target is None:
                continue
            scope = _column_ref(manifest, target, col)
            if scope is None:
                continue
            out.append(
                Fact(
                    scope=scope,
                    value=Nullability.NON_NULL,
                    provenance=Declared(DeclaredSource.DBT_GENERIC_TEST),
                    detail=node.name,
                )
            )
        return out


# NOT NULL is enforced on write by essentially every warehouse, unlike the
# advisory PRIMARY KEY / UNIQUE that several leave unchecked, so the default is
# enforced. The set names adapters known to treat it otherwise (none yet); the
# flag is descriptive provenance, read only by the unenforced-constraint finding.
_NOT_NULL_ADVISORY_ADAPTERS: frozenset[str] = frozenset()


def _not_null_enforced(adapter_type: str) -> bool:
    return adapter_type.lower() not in _NOT_NULL_ADVISORY_ADAPTERS


class _NativeNotNullDiscoverer:
    """Grounds NON_NULL from native ``NOT NULL`` constraints (dbt 1.5+)."""

    def __init__(self, adapter_type: str) -> None:
        self._enforced = _not_null_enforced(adapter_type)

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[Nullability, ColumnRef]]:
        out: list[Fact[Nullability, ColumnRef]] = []
        for node in manifest.nodes.values():
            if node.resource_type is not ResourceType.MODEL:
                continue
            source = SourceRef(SourceKind.MODEL, node.unique_id)
            # Model-level constraints name their columns explicitly.
            out.extend(
                self._fact(source, col, "model-level NOT NULL")
                for c in node.constraints
                if c.type is ConstraintType.NOT_NULL
                for col in c.columns
            )
            # Column-level constraints attach to the column implicitly.
            out.extend(
                self._fact(source, col_name, f"column-level NOT NULL on {col_name}")
                for col_name, col in node.columns.items()
                for c in col.constraints
                if c.type is ConstraintType.NOT_NULL
            )
        return out

    def _fact(self, source: SourceRef, column: str, detail: str) -> Fact[Nullability, ColumnRef]:
        return Fact(
            scope=ColumnRef(source, column.lower()),
            value=Nullability.NON_NULL,
            provenance=NativeConstraint(enforced_on_write=self._enforced),
            detail=detail,
        )


def not_null_test_discoverer() -> FactDiscoverer[Nullability, ColumnRef]:
    return _NotNullTestDiscoverer()


def native_not_null_discoverer(adapter_type: str) -> FactDiscoverer[Nullability, ColumnRef]:
    return _NativeNotNullDiscoverer(adapter_type)


def nullability_property(
    manifest: Manifest,
    *,
    name_to_source: Mapping[str, SourceRef],
    extra: tuple[FactDiscoverer[Nullability, ColumnRef], ...] = (),
) -> Property[Nullability, ColumnRef]:
    """The manifest-backed nullability property: grounding folds the discoverers'
    NON_NULL claims (plus any ``extra``) through the lattice, leaving every
    undeclared column UNKNOWN. No opaque opt-out reader is wired yet, so the
    opaque set is empty."""
    discoverers = (
        not_null_test_discoverer(),
        native_not_null_discoverer(manifest.adapter_type),
        *extra,
    )
    facts = collect(manifest, discoverers, name_to_source=name_to_source)
    return column_property(
        name="nullability",
        lattice=NULLABILITY_LATTICE,
        operators=NULLABILITY_OPERATORS,
        aggregates=NULLABILITY_AGGREGATES,
        ground=grounding(facts, opaque=set(), lat=NULLABILITY_LATTICE),
        semiring=NullabilitySemiring(),
    )
