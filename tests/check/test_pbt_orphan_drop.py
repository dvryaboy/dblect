"""Data-as-judge property test for the referential orphan-drop check.

``orphan_drop_sites`` claims to decide one thing structurally: does a join's row
effect discard a declared foreign key's unmatched child rows. This test asks
duckdb rather than re-deriving the rule: generate a parent table, a child table
carrying one row whose key matches a parent (``matched``) and one whose key
matches none (``orphan``), materialize the join, and assert the check fires iff
the materialized output keeps ``matched`` and lacks ``orphan``.

The join is drawn from a closed grammar (enumerated, not sampled, since
``JoinSide`` is a closed type): every side that carries an ``ON`` clause, crossed
with the child on the accumulated-left side versus freshly joined in. ``CROSS``,
``USING``, ``NATURAL``, and an equality under ``OR`` carry no ``ON`` this reader
decodes, so they are outside this fragment and stay in ``test_orphan_drop.py``'s
documented-miss rows instead.

SEMI and ANTI with the child on the matched (freshly-joined) side never project a
child column at all: the operator only ever returns probe-side rows, so there is
no "this child row survived" fact for such a query's own output to carry. For
those two cases the oracle is the same ``FROM``/``ON`` relaxed to a LEFT JOIN
(which does expose the child column), used only to confirm the generated data is a
genuine match-plus-orphan pair; the check itself must stay silent regardless,
which the row-effect table already says (SEMI/ANTI only ever drop their probe's
unmatched rows, never the matched side's).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import duckdb
import pytest
import sqlglot.expressions as exp
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.check.referential import edges_by_child, orphan_drop_sites
from dblect.lineage.facts.model import Declared, DeclaredSource
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.sql.parse import parse_sql
from dblect.types import ForeignKeyEdge
from tests.lineage._duckdb_oracle import Table, materialized, scalar

_PARENT = SourceRef(SourceKind.MODEL, "model.test.parent")
_CHILD = SourceRef(SourceKind.MODEL, "model.test.child")
_EDGE = ForeignKeyEdge(
    child=ColumnRef(_CHILD, "fk"),
    parent=ColumnRef(_PARENT, "pk"),
    provenance=Declared(DeclaredSource.USER_ASSERTED),
)
_EDGES_BY_CHILD = edges_by_child([_EDGE])
_ALIAS_SOURCE = {"p": _PARENT, "c": _CHILD}


def _ref_of(col: exp.Column) -> ColumnRef | None:
    source = _ALIAS_SOURCE.get(col.table)
    return None if source is None else ColumnRef(source, col.name)


def _fires(sql: str) -> bool:
    tree = cast("exp.Select", parse_sql(sql, dialect="duckdb"))
    return bool(orphan_drop_sites(tree, _EDGES_BY_CHILD, _ref_of))


# id, child position, join keyword, whether this shape can project a child column
_CASES: tuple[tuple[str, str, str, bool], ...] = (
    ("inner-child-left", "left", "inner join", True),
    ("left-child-left", "left", "left join", True),
    ("right-child-left", "left", "right join", True),
    ("full-child-left", "left", "full join", True),
    ("semi-child-left", "left", "semi join", True),
    ("anti-child-left", "left", "anti join", True),
    ("inner-child-right", "right", "inner join", True),
    ("left-child-right", "right", "left join", True),
    ("right-child-right", "right", "right join", True),
    ("full-child-right", "right", "full join", True),
    ("semi-child-right", "right", "semi join", False),
    ("anti-child-right", "right", "anti join", False),
)


def _real_sql(position: str, join: str, *, projectable: bool) -> str:
    """The exact query ``orphan_drop_sites`` analyzes for one grammar case."""
    if position == "left":
        return f"select c.id as id from c {join} p on c.fk = p.pk"
    if projectable:
        return f"select c.id as id from p {join} c on c.fk = p.pk"
    return f"select p.pk as pk from p {join} c on c.fk = p.pk"


_LEFT_RELAXED_ORACLE = "select c.id as id from p left join c on c.fk = p.pk"


@dataclass(frozen=True, slots=True)
class OrphanScenario:
    parent_pks: tuple[int, ...]
    matched_pk: int
    matched_id: int
    orphan_fk: int
    orphan_id: int


@st.composite
def _orphan_scenario(draw: st.DrawFn) -> OrphanScenario:
    parent_pks = draw(st.sets(st.integers(min_value=1, max_value=50), min_size=1, max_size=5))
    matched_pk = draw(st.sampled_from(sorted(parent_pks)))
    # Well outside parent_pks's range, so orphan_fk can never coincide with a real
    # parent key: the child row it labels is an orphan by construction, not by luck.
    orphan_fk = draw(st.integers(min_value=1000, max_value=2000))
    matched_id, orphan_id = draw(
        st.lists(st.integers(min_value=1, max_value=10_000), min_size=2, max_size=2, unique=True)
    )
    return OrphanScenario(tuple(parent_pks), matched_pk, matched_id, orphan_fk, orphan_id)


def _tables(s: OrphanScenario) -> list[Table]:
    return [
        ("p", ("pk",), [(pk,) for pk in s.parent_pks]),
        ("c", ("fk", "id"), [(s.matched_pk, s.matched_id), (s.orphan_fk, s.orphan_id)]),
    ]


@pytest.mark.parametrize(
    ("position", "join", "projectable"),
    [(p, j, proj) for _id, p, j, proj in _CASES],
    ids=[row[0] for row in _CASES],
)
@given(s=_orphan_scenario())
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_orphan_drop_fires_iff_the_warehouse_drops_the_orphan(
    oracle_con: duckdb.DuckDBPyConnection,
    position: str,
    join: str,
    projectable: bool,
    s: OrphanScenario,
) -> None:
    real_sql = _real_sql(position, join, projectable=projectable)
    fires = _fires(real_sql)

    if projectable:
        with materialized(oracle_con, _tables(s), real_sql) as con:
            matched_present = scalar(con, f"select count(*) from _m where id = {s.matched_id}") > 0
            orphan_present = scalar(con, f"select count(*) from _m where id = {s.orphan_id}") > 0
        assert fires == (matched_present and not orphan_present), (
            f"orphan_drop_sites fired={fires} but the warehouse says matched_present="
            f"{matched_present}, orphan_present={orphan_present} for sql={real_sql!r} "
            f"scenario={s!r}"
        )
    else:
        with materialized(oracle_con, _tables(s), _LEFT_RELAXED_ORACLE) as con:
            matched_present = scalar(con, f"select count(*) from _m where id = {s.matched_id}") > 0
            orphan_present = scalar(con, f"select count(*) from _m where id = {s.orphan_id}") > 0
        degenerate = (
            f"the LEFT-relaxed oracle should always show a genuine match-plus-orphan "
            f"split; got matched_present={matched_present}, orphan_present={orphan_present} "
            f"for scenario={s!r} (this would mean the fixture itself is degenerate)"
        )
        assert matched_present, degenerate
        assert not orphan_present, degenerate
        assert fires is False, (
            f"{join} with the child on the matched side never exposes a child row to "
            f"drop, so orphan_drop_sites must stay silent on sql={real_sql!r}"
        )
