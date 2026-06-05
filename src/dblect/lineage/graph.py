"""Data model for the column-level lineage graph.

Per column the graph stores two things:

* ``edges``: the immediate upstream columns this column's projection
  expression references. One step only; the propagator stitches longer
  chains by recursing through the refs stamped on ``exp.Column``s.
* ``expressions``: the sqlglot expression that produced this column at
  the projection level, like ``Alias(Sum(Column))``. (In the K-relations
  literature, "how-provenance.") The propagator walks it top-down.

CTE intermediates, inline-subquery projections, and UNION ALL combined
outputs are all first-class entries: a reference like ``r.combined`` from
an outer SELECT stamps to a ``ColumnRef`` on a synthetic CTE source whose
own projection expression then lives in ``expressions``. UNION ALL
combined outputs carry a ``UnionConfluence`` synthetic node that
plus-folds the per-arm ``ColumnRef``s.

The graph is built by ``builder.py`` per model and merged across the
manifest DAG. Leaf source columns (sources, seeds) appear as ``ColumnRef``
keys with no ``expressions`` entry; ``Property.source`` seeds their value
before propagation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

import sqlglot.expressions as exp
from sqlglot import Expr

# Invariant: a view both yields subjects (output) and reads a subject back
# (input), so it cannot vary in either direction.
S = TypeVar("S", "ColumnRef", "SourceRef")


class LineageView(Protocol[S]):
    """The minimal view the propagator's grounded-fixpoint driver needs of a
    lineage graph, independent of scope.

    A *subject* is a node the propagator annotates (a column for column-scoped
    properties, a relation for relation-scoped ones). ``derivation`` returns the
    sqlglot expression that produced a subject, or ``None`` when the subject is a
    leaf (a source or seed) that grounds from facts. The scope-specific reducer
    is what walks a derivation; the driver only needs to enumerate subjects and
    fetch each one's derivation, so both the column and relation graphs satisfy
    this same protocol.
    """

    def subjects(self) -> Iterable[S]: ...

    def derivation(self, subject: S) -> Expr | None: ...


class SourceKind(StrEnum):
    """What kind of dbt node a ``ColumnRef`` lives on.

    ``MODEL``, ``SOURCE``, ``SEED``, ``SNAPSHOT`` map to real manifest
    entries. ``CTE``, ``UNION``, ``UNION_ARM`` are synthetic kinds the
    builder invents to materialise CTE intermediates, inline-subquery
    projections, UNION ALL combined outputs, and the individual arms as
    first-class graph entries. Detectors that walk the graph should treat
    synthetic kinds as opaque internal scaffolding.
    """

    MODEL = "model"
    SOURCE = "source"
    SEED = "seed"
    SNAPSHOT = "snapshot"
    CTE = "cte"
    UNION = "union"
    UNION_ARM = "union_arm"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Identifier for the node a column belongs to.

    For manifest-backed kinds, ``unique_id`` is the dbt ``unique_id``
    (``model.<project>.<name>``, ``source.<project>.<source_name>.<table_name>``,
    etc.).

    Synthetic-kind id shapes:

    * ``CTE``: ``cte.<model_uid>.<scope_path>``, where ``scope_path`` is a
      dot-joined chain of CTE / derived-table aliases from outermost to
      innermost. Disambiguates same-named CTEs in different scopes.
    * ``UNION``: ``union.<model_uid>.<scope_path>.<col>`` — the combined
      output node for a UNION ALL's column.
    * ``UNION_ARM``: ``union.<model_uid>.<scope_path>#<arm_index>`` —
      individual arm projection, indexed in source order.
    """

    kind: SourceKind
    unique_id: str


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """A specific column on a specific source/model.

    Column names are case-folded (lowercased) at construction sites so JOIN
    qualification and dbt's case-insensitive identifier handling line up. Callers
    that need the original case should keep it alongside; the graph keys on the
    folded form.
    """

    source: SourceRef
    column: str


@dataclass(frozen=True, slots=True)
class ColumnLineageGraph:
    """Per-audit column lineage assembled across the manifest DAG.

    ``edges`` is the immediate upstream relation; the propagator stitches
    longer chains by recursing through ``ColumnRef``s stamped on
    ``exp.Column``s in each projection. ``expressions`` carries the
    sqlglot projection expression the propagator walks. A column with no
    ``expressions`` entry is a leaf (source, seed, upstream-model boundary,
    or unresolved), and ``Property.source`` seeds it.
    """

    edges: Mapping[ColumnRef, frozenset[ColumnRef]]
    expressions: Mapping[ColumnRef, Expr]

    @staticmethod
    def empty() -> ColumnLineageGraph:
        return ColumnLineageGraph(edges={}, expressions={})

    def subjects(self) -> Iterable[ColumnRef]:
        """Every column the propagator should annotate: those with a derivation,
        then leaf columns that appear only as an upstream edge. Order is stable
        (derivations first) and memoisation makes the rest order-insensitive."""
        seen: dict[ColumnRef, None] = dict.fromkeys(self.expressions)
        for col in self.edges:
            seen.setdefault(col, None)
        return tuple(seen)

    def derivation(self, subject: ColumnRef) -> Expr | None:
        """The projection expression that built ``subject``; ``None`` for a leaf."""
        return self.expressions.get(subject)

    def merge(self, other: ColumnLineageGraph) -> ColumnLineageGraph:
        """Union two graphs. Edges union per column; expressions take ``other`` on collision.

        Cross-model composition calls this as it walks the DAG. The "other wins
        on collision" rule means later models can refine earlier per-column
        expressions when they re-derive the same column via richer source
        attribution, which is rare in practice (each output column is built by
        exactly one model) but the rule keeps merge total.
        """
        merged_edges: dict[ColumnRef, frozenset[ColumnRef]] = dict(self.edges)
        for k, v in other.edges.items():
            merged_edges[k] = merged_edges.get(k, frozenset()) | v
        merged_exprs: dict[ColumnRef, Expr] = dict(self.expressions)
        merged_exprs.update(other.expressions)
        return ColumnLineageGraph(edges=merged_edges, expressions=merged_exprs)


# Key on ``exp.Table.meta`` where the relation-graph builder records the
# ``SourceRef`` an upstream table reference resolves to, so the relation reducer
# can recurse into it without re-resolving names. Mirrors how the column builder
# stamps ``exp.Column``s; centralised so builder and reducer stay in sync.
SOURCEREF_META_KEY = "dblect_sourceref"


def attach_source_ref(table: exp.Table, ref: SourceRef) -> None:
    """Stamp ``table`` with the upstream ``SourceRef`` its name resolves to."""
    table.meta[SOURCEREF_META_KEY] = ref


def source_ref_meta(table: exp.Table) -> SourceRef | None:
    """Read the ``SourceRef`` the builder stamped on ``table``; ``None`` if unstamped
    (a CTE or derived-table reference, which the reducer resolves structurally)."""
    meta = table.meta.get(SOURCEREF_META_KEY)
    return meta if isinstance(meta, SourceRef) else None


@dataclass(frozen=True, slots=True)
class RelationLineageGraph:
    """Per-audit relation lineage: each model relation paired with the SQL tree
    that derives it.

    A relation-scoped property annotates a :class:`SourceRef`; its derivation is
    the model's compiled-SQL tree, whose upstream ``exp.Table`` references are
    stamped with the :class:`SourceRef` they resolve to (see
    :func:`attach_source_ref`). A source or seed relation has no derivation and
    grounds from facts, so it appears only as a recursion target, never as a
    derivation key.
    """

    derivations: Mapping[SourceRef, Expr]

    @staticmethod
    def empty() -> RelationLineageGraph:
        return RelationLineageGraph(derivations={})

    def subjects(self) -> Iterable[SourceRef]:
        return tuple(self.derivations)

    def derivation(self, subject: SourceRef) -> Expr | None:
        return self.derivations.get(subject)
