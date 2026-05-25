"""Data model for the column-level lineage graph.

A ``ColumnLineageGraph`` carries two things per output column:

* **where-provenance**: the set of upstream ``ColumnRef``s the output
  ultimately draws from. Edges in the graph.
* **how-provenance**: the sqlglot ``Expression`` that produced this column at
  the projection level (e.g. ``Alias(Sum(Column))``). The propagator walks
  this expression top-down applying per-operator and per-aggregate transfers.

The graph is built by ``builder.py`` from each model's compiled SQL and then
merged across the manifest DAG. Source columns (dbt sources, seeds) appear as
``ColumnRef`` keys with no entry in ``expressions``: they are the leaves where
``Property.source`` is consulted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlglot import Expr


class SourceKind(StrEnum):
    """What kind of dbt node a ``ColumnRef`` lives on."""

    MODEL = "model"
    SOURCE = "source"
    SEED = "seed"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Identifier for the node a column belongs to.

    ``unique_id`` matches dbt's ``unique_id`` convention (``model.<project>.<name>``,
    ``source.<project>.<source_name>.<table_name>``, etc.) when available. For
    in-flight CTEs that haven't been materialised as a node, callers can pass a
    synthetic id (typically ``cte.<query_id>.<cte_name>``); detectors that consume
    the graph should treat unknown prefixes as opaque and skip.
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

    ``edges`` is the flattened where-provenance: every output column points to
    the source-level ``ColumnRef``s it ultimately depends on. Useful for cheap
    queries like "did this output come from X" without invoking the full
    propagator.

    ``expressions`` is the per-column projection ``Expression`` as sqlglot
    parsed it. The propagator walks this expression top-down at each output
    column, dispatching on the expression's subclass to ``Property.operators``
    or ``Property.aggregates``. At leaf ``exp.Column`` nodes the propagator
    recurses into the referenced upstream ``ColumnRef``.

    A ``ColumnRef`` appearing in ``edges`` keys but not in ``expressions`` is a
    leaf source (a dbt source or seed column, or an unresolved reference).
    ``Property.source`` supplies its initial K-annotation.
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
