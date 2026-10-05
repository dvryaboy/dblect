"""Data-as-judge test for the referential orphan-drop check.

``orphan_drop_sites`` decides structurally whether a join discards a declared foreign
key's unmatched child rows. This test asks duckdb instead of re-deriving the rule: a
parent table, and a child table with one row that matches a parent and one orphan.
The check must fire exactly when the materialized join keeps the matched row and
loses the orphan.

Every ``JoinSide`` with an ``ON`` clause, with the child on the probe side and on
the joined-in side. SEMI and ANTI with the child joined in project no child column,
so no output can show a child row surviving; ``test_orphan_drop.py`` pins those two.
"""

from __future__ import annotations

from typing import cast

import duckdb
import pytest
import sqlglot.expressions as exp

from dblect.check.referential import orphan_drop_sites
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.sql.parse import parse_sql
from dblect.types import ForeignKeyEdge
from tests.lineage._duckdb_oracle import Table, materialized, scalar

_PARENT = SourceRef(SourceKind.MODEL, "model.test.parent")
_CHILD = SourceRef(SourceKind.MODEL, "model.test.child")
_EDGE = ForeignKeyEdge(child=ColumnRef(_CHILD, "fk"), parent=ColumnRef(_PARENT, "pk"))
_EDGES = {(_EDGE.child, _EDGE.parent): _EDGE}
_ALIAS_SOURCE = {"p": _PARENT, "c": _CHILD}

_MATCHED_ID, _ORPHAN_ID = 10, 20
_TABLES: list[Table] = [
    ("p", ("pk",), [(1,), (2,)]),
    ("c", ("fk", "id"), [(1, _MATCHED_ID), (999, _ORPHAN_ID)]),
]

_PROBE = "select c.id as id from c {join} p on c.fk = p.pk"
_JOINED = "select c.id as id from p {join} c on c.fk = p.pk"
_CASES = [
    (f"{join}-child-{position}", template.format(join=join))
    for join in ("inner join", "left join", "right join", "full join", "semi join", "anti join")
    for position, template in (("probe", _PROBE), ("joined", _JOINED))
    if not (position == "joined" and join in ("semi join", "anti join"))
]


def _ref_of(col: exp.Column) -> ColumnRef | None:
    source = _ALIAS_SOURCE.get(col.table)
    return None if source is None else ColumnRef(source, col.name)


@pytest.mark.parametrize("sql", [sql for _id, sql in _CASES], ids=[id_ for id_, _sql in _CASES])
def test_orphan_drop_fires_iff_the_warehouse_drops_the_orphan(
    oracle_con: duckdb.DuckDBPyConnection, sql: str
) -> None:
    tree = cast("exp.Select", parse_sql(sql, dialect="duckdb"))
    fires = bool(orphan_drop_sites(tree, _EDGES, _ref_of))
    with materialized(oracle_con, _TABLES, sql) as con:
        matched_kept = scalar(con, f"select count(*) from _m where id = {_MATCHED_ID}") > 0
        orphan_kept = scalar(con, f"select count(*) from _m where id = {_ORPHAN_ID}") > 0
    assert fires == (matched_kept and not orphan_kept)
