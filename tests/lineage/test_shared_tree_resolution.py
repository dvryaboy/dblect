"""The builder writes its resolution back onto the caller's shared tree.

`build_manifest_graph(parsed=...)` resolves each model on a qualified copy and, for a
caller-supplied tree, stamps the resolved ``ColumnRef`` onto the original nodes. A detector
reading that tree then gets the builder's answer, through CTE and derived-table scopes,
without re-walking lexical scope. These pin that the stamps land and resolve through a CTE,
which is the capability the inner-flatten detector consumes.
"""

from __future__ import annotations

import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_manifest_graph
from dblect.lineage.graph import SourceKind
from dblect.lineage.property import resolved_column_ref
from dblect.manifest import Manifest, Node, ResourceType
from dblect.sql import parse_sql

_BQ = profile_for_adapter("bigquery")


def _model(uid: str, sql: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="app",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
    )


def _source(uid: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.SOURCE,
        fqn=(uid,),
        package_name="app",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _unnest_arg_column(tree: Expr) -> exp.Column:
    unnest = next(iter(tree.find_all(exp.Unnest)))
    return next(iter(unnest.find_all(exp.Column)))


def test_unnest_arg_through_cte_resolves_to_the_cte_column() -> None:
    # The metrics shape: a CTE passes a source array through, a later scope unnests it.
    # The unnest argument is in the FROM clause, not a projection, and reads a CTE column.
    sql = (
        "WITH remapped AS (SELECT id, metrics FROM raw_events) "
        "SELECT r.id, m.x FROM remapped r CROSS JOIN UNNEST(r.metrics) AS m"
    )
    tree = parse_sql(sql, dialect="bigquery")
    manifest = Manifest(
        schema_version="v12",
        adapter_type="bigquery",
        nodes={
            n.unique_id: n
            for n in [_source("source.app.raw.raw_events"), _model("model.app.m", sql)]
        },
    )
    build_manifest_graph(manifest, dialect=_BQ.sqlglot_dialect, parsed={"model.app.m": tree})

    ref = resolved_column_ref(_unnest_arg_column(tree))
    assert ref is not None, "unnest argument column was not stamped"
    assert ref.source.kind is SourceKind.CTE
    assert ref.column == "metrics"


def test_unnest_arg_direct_model_ref_resolves_to_the_model_column() -> None:
    # The worked-example shape: the unnest reads directly from a ref'd model.
    stg_sql = "SELECT event_id, tags FROM raw_events"
    mart_sql = "SELECT s.event_id, x FROM stg s CROSS JOIN UNNEST(s.tags) AS x"
    tree = parse_sql(mart_sql, dialect="bigquery")
    nodes = [
        _source("source.app.raw.raw_events"),
        _model("model.app.stg", stg_sql),
        _model("model.app.mart", mart_sql),
    ]
    manifest = Manifest(
        schema_version="v12", adapter_type="bigquery", nodes={n.unique_id: n for n in nodes}
    )
    build_manifest_graph(manifest, dialect=_BQ.sqlglot_dialect, parsed={"model.app.mart": tree})

    ref = resolved_column_ref(_unnest_arg_column(tree))
    assert ref is not None
    assert ref.source.kind is SourceKind.MODEL
    assert ref.source.unique_id == "model.app.stg"
    assert ref.column == "tags"


def test_passing_a_tree_leaves_its_structure_unqualified() -> None:
    # Write-back enriches .meta only; the tree a detector matches on is untouched.
    sql = "SELECT id, amount FROM raw_events"
    tree = parse_sql(sql, dialect="bigquery")
    before = tree.sql(dialect="bigquery")
    manifest = Manifest(
        schema_version="v12",
        adapter_type="bigquery",
        nodes={
            n.unique_id: n
            for n in [_source("source.app.raw.raw_events"), _model("model.app.m", sql)]
        },
    )
    build_manifest_graph(manifest, dialect=_BQ.sqlglot_dialect, parsed={"model.app.m": tree})
    assert tree.sql(dialect="bigquery") == before
