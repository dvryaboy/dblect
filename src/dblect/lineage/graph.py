"""Data model for the column-level lineage graph.

Per output column the graph stores two things:

* ``edges``: the *immediate* upstream columns this column's projection
  expression references. One step only; the propagator stitches longer
  chains by recursing through the column refs stamped on ``exp.Column``s
  in each projection expression.
* ``expressions``: *how* the column was built, the sqlglot expression for
  this column at the projection level, like ``Alias(Sum(Column))``. The
  propagator walks this expression top-down when computing any property.
  (In the K-relations literature this is "how-provenance.")

CTE intermediates, inline-subquery projections, and UNION ALL outputs are
all first-class entries in the graph. A reference like ``r.combined`` from
an outer SELECT into a CTE column stamps to a ``ColumnRef`` whose source
is the synthetic ``cte.<model_uid>.<scope_path>`` shape; that CTE column
in turn has its own projection expression in ``expressions``. UNION ALL
outputs surface as a synthetic ``union.<model_uid>.<scope_path>.<col>``
node whose expression is ``Union(arm0, arm1, ...)``, with each arm
materialised as its own ``ColumnRef`` entry. Properties dispatch on
``exp.Union`` to combine the arms via ``semiring.plus``.

The graph is built by ``builder.py`` from each model's compiled SQL and
then merged across the manifest DAG. Leaf source columns (dbt sources and
seeds) appear as ``ColumnRef`` keys with no entry in ``expressions``:
those are the points where ``Property.source`` is consulted to seed the
value before propagation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlglot import Expr


class SourceKind(StrEnum):
    """What kind of dbt node a ``ColumnRef`` lives on.

    ``MODEL``, ``SOURCE``, ``SEED``, ``SNAPSHOT`` map to real manifest
    entries. ``CTE`` and ``UNION_ARM`` are synthetic kinds the builder
    invents to materialise CTE intermediates, inline-subquery projections,
    and UNION ALL arms as first-class graph entries; detectors that walk
    the graph should treat those kinds as opaque "internal scaffolding"
    rather than user-facing nodes.
    """

    MODEL = "model"
    SOURCE = "source"
    SEED = "seed"
    SNAPSHOT = "snapshot"
    CTE = "cte"
    UNION_ARM = "union_arm"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Identifier for the node a column belongs to.

    For manifest-backed kinds, ``unique_id`` is the dbt ``unique_id``
    (``model.<project>.<name>``, ``source.<project>.<source_name>.<table_name>``,
    etc.).

    For the synthetic kinds the builder invents:

    * ``CTE``: ``cte.<model_uid>.<scope_path>`` where ``scope_path`` is a
      dot-joined chain of CTE aliases / subquery aliases from outermost to
      innermost (e.g. ``cte.model.test.m.outer_cte.inner_cte``). This is
      stable across builds for the same SQL and disambiguates CTEs with
      the same alias in different lexical scopes.
    * ``UNION_ARM``: ``union.<model_uid>.<scope_path>.<col>`` for a
      UNION's combined output node, or
      ``union.<model_uid>.<scope_path>.<col>#<arm_index>`` for an
      individual arm projection. Arms are indexed in the order they appear
      in the SQL.
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

    ``edges`` is the immediate upstream relation: every column points to
    the columns its projection expression directly references. One step
    only. The propagator stitches longer chains by recursing through the
    ``ColumnRef`` stamped on each ``exp.Column`` in the projection.

    ``expressions`` is the per-column projection expression as sqlglot
    parsed it. The propagator walks this expression top-down for each
    column, dispatching on the expression type to ``Property.operators``
    or ``Property.aggregates`` and recursing into the upstream column at
    each ``exp.Column`` leaf via its stamped ``ColumnRef``.

    A column that appears in ``edges`` keys but not in ``expressions`` is
    a leaf source (a dbt source or seed column, an upstream-model column
    at a per-model build boundary, or an unresolved reference).
    ``Property.source`` supplies its starting value before propagation.
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
