"""Empirical soundness PBT for nullability: the oracle is execution, not re-derivation.

The soundness invariant for nullability is the analogue of "a promoted key is never
wrong": **no column the propagator calls ``NON_NULL`` is ever null in materialized
data.** This test generates a model that joins two sources under each join type,
runs the analyzer to get every output column's nullability, materializes the model
against generated data in duckdb, and asserts every ``NON_NULL`` column has no nulls
in the result.

The teeth come from the join. Each source column carries a ``not_null`` test, so the
analyzer grounds it ``NON_NULL`` at its source. Under an outer join the optional
side's columns are padded with nulls for unmatched rows, so a column that traces to
a ``not_null`` source is nonetheless null in the output. Until the analyzer accounts
for the outer-join optional side, it calls those columns ``NON_NULL`` and this test
fails on exactly the rows the join padded; once it does, the optional-side columns
read ``NULLABLE`` and drop out of the check, while the required side stays sound.

The generator stays inside an executable grammar and the data is non-null in every
``not_null`` column (so the declarations hold), following the discipline of the
uniqueness soundness PBT it shares an oracle with.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.adapters import profile_for_adapter
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties import Nullability
from dblect.lineage.properties.nullability import activated_nullability
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType
from tests.lineage._duckdb_oracle import Table, materialized, scalar

_DUCKDB = profile_for_adapter("duckdb")

_MODEL_UID = "model.test.m"
_OUTPUT_COLS = ("bk", "bv", "jk", "jv")
_JOIN_SQL = {
    "inner": "INNER JOIN",
    "left": "LEFT JOIN",
    "right": "RIGHT JOIN",
    "full": "FULL JOIN",
}


@dataclass(frozen=True)
class NullScenario:
    join: str
    rows_bt: tuple[tuple[int, int], ...]  # (bk, bv)
    rows_jt: tuple[tuple[int, int], ...]  # (jk, jv)


@st.composite
def _rows(draw: st.DrawFn) -> tuple[tuple[int, int], ...]:
    """Rows of two small non-null ints. The small domain makes the join both match and
    miss, so an outer join produces padded nulls on the optional side."""
    n = draw(st.integers(min_value=0, max_value=6))
    return tuple((draw(st.integers(0, 3)), draw(st.integers(0, 3))) for _ in range(n))


@st.composite
def _null_scenario(draw: st.DrawFn) -> NullScenario:
    return NullScenario(
        join=draw(st.sampled_from(tuple(_JOIN_SQL))),
        rows_bt=draw(_rows()),
        rows_jt=draw(_rows()),
    )


def _null_sql(s: NullScenario) -> str:
    return f"SELECT bt.bk, bt.bv, jt.jk, jt.jv FROM bt {_JOIN_SQL[s.join]} jt ON bt.bk = jt.jk"


def _source(name: str) -> Node:
    return Node(
        unique_id=f"source.test.raw.{name}",
        name=name,
        resource_type=ResourceType.SOURCE,
        fqn=("test", name),
        package_name="test",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _not_null_test(source_name: str, column: str) -> Node:
    target = f"source.test.raw.{source_name}"
    return Node(
        unique_id=f"test.test.{source_name}_{column}_not_null",
        name=f"{source_name}_{column}_not_null",
        resource_type=ResourceType.OTHER,
        fqn=("test", f"{source_name}_{column}_not_null"),
        package_name="test",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="not_null", kwargs={"column_name": column}),
        attached_node=target,
    )


def _manifest(s: NullScenario) -> Manifest:
    sql = _null_sql(s)
    nodes = [
        _source("bt"),
        _source("jt"),
        _not_null_test("bt", "bk"),
        _not_null_test("bt", "bv"),
        _not_null_test("jt", "jk"),
        _not_null_test("jt", "jv"),
        Node(
            unique_id=_MODEL_UID,
            name="m",
            resource_type=ResourceType.MODEL,
            fqn=("test", "m"),
            package_name="test",
            schema="analytics",
            raw_code=sql,
            compiled_code=sql,
            original_file_path=None,
            columns={},
            depends_on=frozenset({"source.test.raw.bt", "source.test.raw.jt"}),
        ),
    ]
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


@given(_null_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_non_null_columns_are_never_null_over_materialized_rows(s: NullScenario) -> None:
    """Every output column the analyzer calls NON_NULL has no nulls in the duckdb
    materialization. The check never recomputes which columns should be nullable; the
    data is the judge."""
    anns = activated_nullability(_manifest(s), _DUCKDB)
    model = SourceRef(SourceKind.MODEL, _MODEL_UID)
    non_null_cols = [
        c for c in _OUTPUT_COLS if anns[ColumnRef(model, c)].value is Nullability.NON_NULL
    ]
    tables: list[Table] = [("bt", ("bk", "bv"), s.rows_bt), ("jt", ("jk", "jv"), s.rows_jt)]
    with materialized(tables, _null_sql(s)) as con:
        for col in non_null_cols:
            nulls = scalar(con, f"SELECT COUNT(*) FROM _m WHERE {col} IS NULL")
            assert nulls == 0, (
                f"column {col} claimed NON_NULL but has {nulls} null rows "
                f"for sql={_null_sql(s)!r} bt={s.rows_bt!r} jt={s.rows_jt!r}"
            )
