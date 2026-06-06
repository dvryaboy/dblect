"""The nullability-consuming detector: GROUP BY on an inherited-nullable key.

The detector fires when a model groups by a column that the nullability property
proved NULLABLE upstream, so the unmatched rows collapse into a phantom NULL group
the consumer rarely models. It is the cross-model complement to the structural
``null_group_after_outer_join``: that one needs the outer join in the same SELECT,
this one fires when the nullability was inherited from an upstream model, which the
local AST cannot see. The two do not overlap, because this detector only looks at
single-source, join-free scopes (so any nullability in the group key came from
upstream, never from a join in the scope itself).

These pin the contract by example: a cross-model nullable key fires, a non-null key
stays silent, and a local outer join in the same scope is left to the structural
detector. One empirical check materializes the chain in duckdb and confirms the
flagged group key really does carry a NULL bucket.
"""

from __future__ import annotations

import duckdb

from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType
from dblect.nullability.detector import make_nullability_detectors
from dblect.sql import FindingKind, parse_sql


def _source(name: str) -> Node:
    return Node(
        unique_id=f"source.shop.raw.{name}",
        name=name,
        resource_type=ResourceType.SOURCE,
        fqn=("shop", name),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _model(name: str, sql: str, *, depends_on: frozenset[str]) -> Node:
    return Node(
        unique_id=f"model.shop.{name}",
        name=name,
        resource_type=ResourceType.MODEL,
        fqn=("shop", name),
        package_name="shop",
        schema="analytics",
        raw_code=sql,
        compiled_code=sql,
        original_file_path=None,
        columns={},
        depends_on=depends_on,
    )


def _not_null(source_name: str, column: str) -> Node:
    target = f"source.shop.raw.{source_name}"
    return Node(
        unique_id=f"test.shop.{source_name}_{column}_not_null",
        name=f"{source_name}_{column}_not_null",
        resource_type=ResourceType.OTHER,
        fqn=("shop", f"{source_name}_{column}_not_null"),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="not_null", kwargs={"column_name": column}),
        attached_node=target,
    )


# stg LEFT JOINs lkp, so stg.tag is the optional side: NULLABLE downstream even though
# lkp.tag is declared not_null. stg.id comes from the required side: NON_NULL.
_STG_SQL = "SELECT a.id AS id, b.tag AS tag FROM base a LEFT JOIN lkp b ON a.fk = b.id"


def _manifest(mart_sql: str) -> Manifest:
    nodes = [
        _source("base"),
        _source("lkp"),
        _not_null("base", "id"),
        _not_null("lkp", "id"),
        _not_null("lkp", "tag"),
        _model("stg", _STG_SQL, depends_on=frozenset({"source.shop.raw.base", "source.shop.raw.lkp"})),
        _model("mart", mart_sql, depends_on=frozenset({"model.shop.stg"})),
    ]
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def _kinds(mart_sql: str) -> list[FindingKind]:
    manifest = _manifest(mart_sql)
    (detector,) = make_nullability_detectors(manifest)
    return [f.kind for f in detector(parse_sql(mart_sql, dialect="duckdb"))]


def test_group_by_inherited_nullable_key_fires() -> None:
    # ``stg.tag`` is nullable because ``stg`` left-joined it. Grouping by it downstream
    # collapses unmatched rows into a phantom NULL group.
    kinds = _kinds("SELECT tag, count(*) AS n FROM stg GROUP BY tag")
    assert FindingKind.NULL_GROUP_ON_NULLABLE_KEY in kinds


def test_group_by_non_null_key_stays_silent() -> None:
    # ``stg.id`` rode in from the required side of the join, so it is NON_NULL: no group
    # hazard.
    kinds = _kinds("SELECT id, count(*) AS n FROM stg GROUP BY id")
    assert FindingKind.NULL_GROUP_ON_NULLABLE_KEY not in kinds


def test_local_outer_join_group_by_is_left_to_the_structural_detector() -> None:
    # The outer join and the GROUP BY are in the same scope, which the structural
    # ``null_group_after_outer_join`` detector owns. This detector only reasons about
    # single-source, join-free scopes, so it stays silent here (no double-flagging).
    kinds = _kinds(
        "SELECT b.tag AS tag, count(*) AS n FROM base a LEFT JOIN lkp b ON a.fk = b.id GROUP BY b.tag"
    )
    assert FindingKind.NULL_GROUP_ON_NULLABLE_KEY not in kinds


def test_flagged_group_key_really_carries_a_null_bucket() -> None:
    # Empirical: materialize base -> stg -> mart with an unmatched row, confirm the
    # finding fires and the grouped output genuinely has a NULL key.
    mart_sql = "SELECT tag, count(*) AS n FROM stg GROUP BY tag"
    assert FindingKind.NULL_GROUP_ON_NULLABLE_KEY in _kinds(mart_sql)

    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE base (id INTEGER, fk INTEGER)")
        con.execute("CREATE TABLE lkp (id INTEGER, tag INTEGER)")
        con.executemany("INSERT INTO base VALUES (?, ?)", [[1, 99]])  # fk 99 has no lkp match
        con.executemany("INSERT INTO lkp VALUES (?, ?)", [[1, 7]])
        con.execute(f"CREATE TABLE stg AS {_STG_SQL}")
        con.execute(f"CREATE TABLE mart AS {mart_sql}")
        null_groups = con.execute("SELECT count(*) FROM mart WHERE tag IS NULL").fetchone()
        assert null_groups is not None
        assert null_groups[0] == 1  # the unmatched row formed a phantom NULL group
    finally:
        con.close()
