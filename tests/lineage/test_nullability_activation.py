"""Activation of conditional ``not_null`` facts against the predicate flow.

A ``where``-filtered ``not_null`` test grounds a conditional NON_NULL: captured,
carried across relations, and promoted at a scope whose accumulated row filter
implies the test's predicate. Nullability is column-scoped, so activation rides a
relation-level carrier (a column is non-null *under a predicate*) that renames the
column and predicate through projections exactly as the uniqueness carrier does,
then folds NON_NULL into the column annotations. These pin that promotion at the
annotation boundary; no detector consumes nullability yet, so there is no
finding-level case.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.nullability import Nullability, activated_nullability
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType

_DUCKDB = profile_for_adapter("duckdb")


def _model(uid: str, sql: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
        constraints=(),
    )


def _source(uid: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.SOURCE,
        fqn=(uid,),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _not_null(uid: str, *, column: str, target: str, where: str | None = None) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.OTHER,
        fqn=(uid,),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="not_null", kwargs={"column_name": column}, where=where),
        attached_node=target,
    )


def _activated(*nodes: Node) -> Mapping[ColumnRef, Nullability]:
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )
    return {ref: ann.value for ref, ann in activated_nullability(manifest, _DUCKDB).items()}


def _model_col(uid: str, col: str) -> ColumnRef:
    return ColumnRef(SourceRef(SourceKind.MODEL, uid), col)


def test_conditional_not_null_activates_cross_model() -> None:
    # The test is on the source; the model applies the implying filter. ``email``
    # carries to the model and promotes to NON_NULL.
    res = _activated(
        _source("source.shop.raw.events"),
        _not_null("test.nn", column="email", target="source.shop.raw.events", where="amount > 0"),
        _model("model.shop.dim", "SELECT email, amount FROM events WHERE amount > 0"),
    )
    assert res[_model_col("model.shop.dim", "email")] is Nullability.NON_NULL


def test_conditional_not_null_activates_under_a_narrower_filter() -> None:
    res = _activated(
        _source("source.shop.raw.events"),
        _not_null("test.nn", column="email", target="source.shop.raw.events", where="amount > 0"),
        _model("model.shop.dim", "SELECT email, amount FROM events WHERE amount > 10"),
    )
    assert res[_model_col("model.shop.dim", "email")] is Nullability.NON_NULL


def test_conditional_not_null_not_activated_without_a_filter() -> None:
    res = _activated(
        _source("source.shop.raw.events"),
        _not_null("test.nn", column="email", target="source.shop.raw.events", where="amount > 0"),
        _model("model.shop.dim", "SELECT email, amount FROM events"),
    )
    assert res.get(_model_col("model.shop.dim", "email")) is not Nullability.NON_NULL


def test_within_relation_conditional_not_null_activates() -> None:
    # The test is on the model that applies its own filter.
    res = _activated(
        _source("source.shop.raw.events"),
        _model("model.shop.dim", "SELECT email, amount FROM events WHERE amount > 0"),
        _not_null("test.nn", column="email", target="model.shop.dim", where="amount > 0"),
    )
    assert res[_model_col("model.shop.dim", "email")] is Nullability.NON_NULL


def test_predicate_column_renames_through_the_projection() -> None:
    # ``amount`` is projected as ``amt``; the carried predicate renames with it and
    # matches the model's flow, so activation still fires.
    res = _activated(
        _source("source.shop.raw.events"),
        _not_null("test.nn", column="email", target="source.shop.raw.events", where="amount > 0"),
        _model("model.shop.dim", "SELECT email, amount AS amt FROM events WHERE amount > 0"),
    )
    assert res[_model_col("model.shop.dim", "email")] is Nullability.NON_NULL


def test_unconditional_not_null_is_unaffected() -> None:
    res = _activated(
        _source("source.shop.raw.events"),
        _not_null("test.nn", column="email", target="model.shop.dim"),
        _model("model.shop.dim", "SELECT email FROM events"),
    )
    assert res[_model_col("model.shop.dim", "email")] is Nullability.NON_NULL
