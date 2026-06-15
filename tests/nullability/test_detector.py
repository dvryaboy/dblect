"""The nullability-consuming detectors: a NULL-sensitive construct on an
inherited-nullable column.

Three detectors share one shape: they fire when a column the nullability property
proved NULLABLE *upstream* sits in a position where the null silently changes the
result (a GROUP BY phantom bucket, a join key that never matches, a NOT IN that goes
empty). They are the cross-model complement to the structural detectors: those need
the outer join in the same SELECT, these fire on nullability inherited from an
upstream model, which the local AST cannot see. They never double-flag the structural
layer, because they read the *upstream* relation's nullability rather than anything a
join in the local scope introduces.

The contract is one parametrized table (each construct fires on the nullable key and
stays silent on the non-null one), so a new construct is a new row rather than a new
pair of near-identical tests. One empirical check materializes the chain in duckdb and
confirms the flagged group key really carries a NULL bucket.
"""

from __future__ import annotations

import duckdb
import pytest

from dblect.adapters import profile_for_adapter
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType
from dblect.nullability.detector import make_nullability_detectors
from dblect.sql import FindingKind, parse_sql

_DUCKDB = profile_for_adapter("duckdb")


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
        _source("other"),
        _not_null("base", "id"),
        _not_null("lkp", "id"),
        _not_null("lkp", "tag"),
        _not_null("other", "k"),
        _model(
            "stg", _STG_SQL, depends_on=frozenset({"source.shop.raw.base", "source.shop.raw.lkp"})
        ),
        _model(
            "mart",
            mart_sql,
            depends_on=frozenset({"model.shop.stg", "source.shop.raw.other"}),
        ),
    ]
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def _kinds(mart_sql: str) -> list[FindingKind]:
    """Every nullability finding the detectors raise on ``mart``."""
    detectors = make_nullability_detectors(_manifest(mart_sql), _DUCKDB)
    tree = parse_sql(mart_sql, dialect="duckdb")
    return [f.kind for detector in detectors for f in detector(tree)]


# ``stg.tag`` is nullable upstream (the optional side of stg's LEFT JOIN); ``stg.id`` and
# ``other.k`` are NON_NULL. Each detector fires on the nullable key and stays silent on the
# non-null one. The local-outer-join GROUP BY is left to the structural detector, since
# this layer reasons only about single-source, join-free scopes.
_CASES: list[tuple[str, str, bool]] = [
    ("group-by/nullable", "SELECT tag, count(*) AS n FROM stg GROUP BY tag", True),
    ("group-by/non-null", "SELECT id, count(*) AS n FROM stg GROUP BY id", False),
    (
        "group-by/local-join",
        "SELECT b.tag AS tag, count(*) AS n FROM base a LEFT JOIN lkp b ON a.fk = b.id GROUP BY b.tag",
        False,
    ),
    ("join/nullable", "SELECT s.id FROM other o JOIN stg s ON o.k = s.tag", True),
    ("join/non-null", "SELECT s.id FROM other o JOIN stg s ON o.k = s.id", False),
    ("not-in/nullable", "SELECT id FROM stg WHERE id NOT IN (SELECT tag FROM stg)", True),
    ("not-in/non-null", "SELECT id FROM stg WHERE id NOT IN (SELECT id FROM stg)", False),
]
_KIND_OF = {
    "group-by": FindingKind.NULL_GROUP_ON_NULLABLE_KEY,
    "join": FindingKind.JOIN_ON_NULLABLE_KEY,
    "not-in": FindingKind.NOT_IN_NULLABLE_SUBQUERY,
}


@pytest.mark.parametrize(
    ("sql", "kind", "fires"),
    [(sql, _KIND_OF[name.split("/")[0]], fires) for name, sql, fires in _CASES],
    ids=[name for name, _sql, _fires in _CASES],
)
def test_detector_fires_only_on_an_inherited_nullable_key(
    sql: str, kind: FindingKind, fires: bool
) -> None:
    assert (kind in _kinds(sql)) is fires


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
