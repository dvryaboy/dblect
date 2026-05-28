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

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlglot import Expr


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
