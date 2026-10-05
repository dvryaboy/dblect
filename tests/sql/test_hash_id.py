"""When a hash id determines the columns it hashes.

``h = HASH(enc(x1..xn))`` supports ``h -> {x1..xn}`` under one named assumption, the one
practitioners make when they use a hash as an id: hashed ids are injective. Any
deterministic hash family and any encoding of the inputs qualify. The recognizer's job is
the boundary that does cause wrong answers, not collisions: a hash that is reduced before
it becomes the column (modulo, truncation, masks, shifts, division) is a bucket, not an
id, and two inputs can share it by pigeonhole. Each wrapper around the digest is decided in
a closed table, and the pre-image must be exactly columns and constants.
"""

from __future__ import annotations

import duckdb
import pytest
import sqlglot.expressions as exp
from sqlglot import Expr

from dblect.sql import parse_sql
from dblect.sql._sqlglot import column_name
from dblect.sql.hash_id import injective_hash_inputs


def _inputs(sql: str, dialect: str = "duckdb") -> tuple[str, ...] | None:
    select = parse_sql(f"SELECT {sql} AS h FROM t", dialect=dialect)
    assert isinstance(select, exp.Select)
    proj = select.expressions[0]
    assert isinstance(proj, exp.Alias)
    assert isinstance(proj.this, Expr)
    cols = injective_hash_inputs(proj.this)
    return None if cols is None else tuple(column_name(c) for c in cols)


_TUVA_FIELD = (
    "(case when {x} is null then 'N' else "
    "('V' || replace(replace(cast({x} as TEXT), '%', '%25'), '|', '%7C')) end)"
)
_TUVA = "md5(" + " || '|' || ".join(_TUVA_FIELD.format(x=x) for x in ("a", "b")) + ")"
_SURROGATE_KEY = (
    "md5(cast(coalesce(cast(a as text), '_dbt_utils_surrogate_key_null_') || '-' || "
    "coalesce(cast(b as text), '_dbt_utils_surrogate_key_null_') as text))"
)


# --- accepted: any hash family over any encoding of columns --------------------------


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        (_TUVA, "duckdb", ("a", "b")),
        (_SURROGATE_KEY, "duckdb", ("a", "b")),
        ("md5(concat(a, '|', b))", "postgres", ("a", "b")),
        ("md5(concat_ws('-', a, b))", "duckdb", ("a", "b")),
        ("md5(a || b)", "duckdb", ("a", "b")),
        ("md5(coalesce(a, 'x'))", "duckdb", ("a",)),
        ("md5(coalesce(a, 'x', 'y'))", "duckdb", ("a",)),
        ("md5(coalesce(cast(a as varchar), '' || 'x'))", "duckdb", ("a",)),
        ("md5(cast(ts as varchar))", "duckdb", ("ts",)),
        ("md5(cast(cast(a as string) as text))", "duckdb", ("a",)),
        ("md5(replace(a, '|', '%7C'))", "duckdb", ("a",)),
        ("md5(replace(replace(a, '%', '%25'), '|', '%7C'))", "duckdb", ("a",)),
        ("md5(concat_ws('-', a, b))", "snowflake", ("a", "b")),
        ("hashtextextended(a, 0)", "postgres", ("a",)),
        ("md5(cast(a as text))", "duckdb", ("a",)),
        ("md5('kind' || a)", "duckdb", ("a",)),
        ("md5(t.a)", "duckdb", ("a",)),
        ("sha1(a)", "duckdb", ("a",)),
        ("sha2(a, 256)", "snowflake", ("a",)),
        ("sha2(a, 224)", "snowflake", ("a",)),
        ("farm_fingerprint(concat(a, b))", "bigquery", ("a", "b")),
        ("hash(a, b)", "snowflake", ("a", "b")),
        ("hash(a)", "duckdb", ("a",)),
        ("xxhash64(a)", "duckdb", ("a",)),
        # Wrappers that keep the full digest.
        ("lower(md5(a))", "duckdb", ("a",)),
        ("upper(md5(a))", "duckdb", ("a",)),
        ("to_hex(md5(a))", "bigquery", ("a",)),
        ("lower(to_hex(md5(a)))", "bigquery", ("a",)),
        ("cast(hash(a) as varchar)", "duckdb", ("a",)),
        ("(md5(a))", "duckdb", ("a",)),
    ],
)
def test_a_hash_of_columns_determines_them(
    sql: str, dialect: str, expected: tuple[str, ...]
) -> None:
    assert _inputs(sql, dialect) == expected


def test_a_hash_of_only_constants_determines_nothing() -> None:
    assert _inputs("md5('a')") is None


# --- the one boundary: a reduced hash is a bucket --------------------------------------

# Reductions the SQL can run in duckdb: distinct inputs 0..49 must produce fewer distinct
# values than inputs, so the reduced column cannot determine its input.
_RUNNABLE_REDUCTIONS = [
    "hash(a) % 4",
    "mod(hash(a), 4)",
    "abs(hash(a)) % 4",
    "substr(md5(cast(a as text)), 1, 1)",
    "left(md5(cast(a as text)), 1)",
    "right(md5(cast(a as text)), 1)",
    "hash(a) & 3",
    "hash(a) >> 60",
    "hash(a) // 4611686018427387904",
]
# Reductions with no portable witness here (overflow errors, ignored sizes, rare sign
# collisions): declined all the same.
_OTHER_REDUCTIONS = [
    "abs(hash(a))",
    "cast(hash(a) as tinyint)",
    "try_cast(hash(a) as integer)",
    "hash(a) | 1",
    "hash(a) / 4611686018427387904",
    "hash(a) << 60",
    "round(hash(a), -3)",
    "hash(a) + 1",
    "floor(hash(a) / 7)",
]


@pytest.mark.parametrize("sql", _RUNNABLE_REDUCTIONS)
def test_a_reduced_hash_is_a_bucket_not_an_id(sql: str) -> None:
    assert _inputs(sql) is None
    con = duckdb.connect(":memory:")
    got = con.execute(
        f"SELECT count(DISTINCT h) FROM (SELECT {sql} AS h FROM range(50) t(a))"
    ).fetchone()
    assert got is not None
    assert got[0] < 50


@pytest.mark.parametrize("sql", _OTHER_REDUCTIONS)
def test_other_reducing_wrappers_are_declined(sql: str) -> None:
    assert _inputs(sql) is None


def test_a_sized_text_cast_of_the_digest_is_a_truncation() -> None:
    assert _inputs("cast(md5(a) as varchar(8))", "postgres") is None
    assert _inputs("cast(md5(a) as varchar)", "postgres") == ("a",)


def test_a_reduction_nested_under_a_preserving_wrapper_is_still_declined() -> None:
    assert _inputs("lower(substr(md5(a), 1, 8))") is None
    assert _inputs("cast(hash(a) % 4 as varchar)") is None


# --- the pre-image is exactly columns and constants ------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "md5(lower(a))",  # the hash fixes lower(a), not a
        "md5(trim(a))",
        "md5(substr(a, 1, 3))",
        "md5(a + 1)",
        "md5(a || random())",
        "md5(a || uuid())",
        "md5(a || cast(current_timestamp as text))",
        "md5(a || cast(now() as text))",
        "md5(a || (select max(x) from u))",
        "md5(a || cast(max(b) as text))",
        "md5(a || cast(row_number() over () as text))",
        "md5(f(a))",
    ],
)
def test_a_pre_image_that_is_not_exactly_columns_and_constants_is_declined(sql: str) -> None:
    assert _inputs(sql) is None


# --- a condition buckets the columns it tests ------------------------------------------
# A column read only by a CASE/IF condition or an IS test reaches the hash as a truth
# value, so the hash fixes the bucket, not the column. Null-tagging is the one condition
# that keeps the column: it tests a column the branches also carry.


@pytest.mark.parametrize(
    "sql",
    [
        "md5(case when a > 5 then 'big' else 'small' end)",
        "md5(cast(a is null as text))",
        "md5(if(a > 5, 'x', 'y'))",
    ],
)
def test_a_hash_of_a_condition_is_a_bucket_not_an_id(sql: str) -> None:
    assert _inputs(sql) is None
    con = duckdb.connect(":memory:")
    got = con.execute(
        f"SELECT count(DISTINCT h) FROM (SELECT {sql} AS h FROM range(50) t(a))"
    ).fetchone()
    assert got is not None
    assert got[0] < 50


@pytest.mark.parametrize(
    "sql",
    [
        "md5(case when a > 5 then b else c end)",  # each row's hash fixes only one branch
        "md5(case when b is null then 'N' else a end)",  # b is only tested
        "md5(case when a is null then 'N' else b end)",  # a is only tested
    ],
)
def test_a_column_a_condition_only_tests_is_not_determined(sql: str) -> None:
    assert _inputs(sql) is None


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("md5(case when a is null then 'N' else a end)", ("a",)),
        ("md5(case when a is not null then a else 'N' end)", ("a",)),
        ("md5(if(a is null, 'N', a))", ("a",)),
    ],
)
def test_null_tagging_keeps_the_column(sql: str, expected: tuple[str, ...]) -> None:
    assert _inputs(sql) == expected


# --- an encoding that discards information from an input is declined -------------------
# Separator collisions and NULL sentinels stay under the injectivity assumption. These
# shapes lose an input's value outright, so the FD would be a wrong answer.


@pytest.mark.parametrize(
    "sql",
    [
        "md5(coalesce(a, b))",  # b never reaches the hash while a is non-null
        "md5(coalesce(a, b, 'x'))",
        "md5(coalesce(coalesce(a, 'x'), b))",
        "md5(coalesce('x', a))",  # a is only a fallback to a constant
    ],
)
def test_coalesce_with_a_column_fallback_is_declined(sql: str) -> None:
    assert _inputs(sql) is None


def test_coalesce_with_a_column_fallback_collides_on_data() -> None:
    con = duckdb.connect(":memory:")
    got = con.execute(
        "SELECT count(DISTINCT md5(cast(coalesce(a, b) as varchar))) "
        "FROM (VALUES (NULL, 1), (1, 2)) t(a, b)"
    ).fetchone()
    assert got is not None
    assert got[0] == 1


@pytest.mark.parametrize(
    "sql",
    [
        "md5(cast(ts as date))",
        "md5(cast(a as varchar(3)))",
        "md5(cast(a as tinyint))",
        "md5(cast(a as boolean))",
        "md5(cast(a as decimal(5, 0)))",
        "md5(cast(a as int))",
        "md5(cast(a as bigint))",
        "md5(cast(a as double))",
        "md5(cast(a as date))",
        "md5(try_cast(a as tinyint))",
        "md5(cast(a as char(1)))",
        "md5(coalesce(cast(a as tinyint), 'x'))",
    ],
)
def test_a_lossy_cast_in_the_pre_image_is_declined(sql: str) -> None:
    # Postgres keeps the length of ``varchar(3)``; duckdb's parser drops it.
    assert _inputs(sql, "postgres") is None


@pytest.mark.parametrize(
    "lossy",
    [
        "cast(a as boolean)",
        "cast(a / 25 as integer)",
        "cast(date '2020-01-01' + cast(a as integer) as date) // 1000000"
        if False
        else "cast(timestamp '2020-01-01 00:00:00' + to_seconds(a) as date)",
    ],
)
def test_a_lossy_cast_collides_on_data(lossy: str) -> None:
    con = duckdb.connect(":memory:")
    got = con.execute(
        f"SELECT count(DISTINCT h) FROM (SELECT md5(cast({lossy} as varchar)) AS h "
        "FROM range(50) t(a))"
    ).fetchone()
    assert got is not None
    assert got[0] < 50


@pytest.mark.parametrize(
    "sql",
    [
        "md5(replace(a, 'x', ''))",  # deletion merges 'ax' and 'a'
        "md5(replace(a, 'x'))",
        "md5(replace(a, b, '_'))",
        "md5(replace(a, 'x', b))",
        "md5(replace(replace(a, '%', '%25'), '|', ''))",
    ],
)
def test_a_replace_that_deletes_or_is_not_literal_is_declined(sql: str) -> None:
    assert _inputs(sql) is None


def test_replace_deletion_collides_on_data() -> None:
    con = duckdb.connect(":memory:")
    got = con.execute(
        "SELECT count(DISTINCT md5(replace(a, 'x', ''))) FROM (VALUES ('ax'), ('a')) t(a)"
    ).fetchone()
    assert got is not None
    assert got[0] == 1


def test_concat_ws_with_a_column_separator_is_declined() -> None:
    assert _inputs("md5(concat_ws(s, a, b))") is None
    assert _inputs("md5(concat_ws('-' || s, a, b))") is None


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("crc32(a)", "duckdb"),
        ("crc32c(a)", "duckdb"),
        ("hashtext(a)", "postgres"),
        ("xxhash32(a)", "duckdb"),
        ("murmur_hash3_32(a)", "duckdb"),
    ],
)
def test_a_32_bit_digest_is_a_bucket_and_declined(sql: str, dialect: str) -> None:
    assert _inputs(sql, dialect) is None
