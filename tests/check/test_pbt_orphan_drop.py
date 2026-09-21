"""Data-as-judge property test for the referential orphan-drop check.

``orphan_drop_sites`` claims one thing structurally: a join's row effect discards
a declared foreign key's unmatched child rows. This asks duckdb rather than
re-deriving the rule: generate a parent table and a child table carrying one row
that matches a parent (``matched``) and one that matches none (``orphan``),
materialize the join, and assert that wherever the check fires, the warehouse
confirms the drop really happened (``matched`` survives, ``orphan`` does not).
``assert_no_over_claims`` is exactly this "the analysis never claims more than
the data supports" shape.

The six shapes below are every ``(JoinSide, child position)`` combination
``orphan_drop_sites`` claims fires (see ``test_orphan_drop.py``'s structural
table for the full closed grammar, firing and silent alike); a silent shape has
no drop to confirm against data, so it stays a structural fact pinned there and
in ``test_join_row_effects.py``, not a data-as-judge claim here.
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
from tests.lineage._duckdb_oracle import Table, assert_no_over_claims

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


# every (JoinSide, child position) shape the structural table says fires, one
# real query per row
_FIRING_SQL: tuple[tuple[str, str], ...] = (
    ("inner-child-left", "select c.id as id from c inner join p on c.fk = p.pk"),
    ("right-child-left", "select c.id as id from c right join p on c.fk = p.pk"),
    ("semi-child-left", "select c.id as id from c semi join p on c.fk = p.pk"),
    ("inner-child-right", "select c.id as id from p inner join c on c.fk = p.pk"),
    ("left-child-right", "select c.id as id from p left join c on c.fk = p.pk"),
)


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
    "sql", [sql for _id, sql in _FIRING_SQL], ids=[row[0] for row in _FIRING_SQL]
)
@given(s=_orphan_scenario())
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_orphan_drop_never_over_claims_a_drop_that_did_not_happen(
    oracle_con: duckdb.DuckDBPyConnection, sql: str, s: OrphanScenario
) -> None:
    assert _fires(sql), f"{sql!r} is expected to be in the firing grammar"

    def violations(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        matched_present = con.execute(
            f"select count(*) from _m where id = {s.matched_id}"
        ).fetchone()
        orphan_present = con.execute(f"select count(*) from _m where id = {s.orphan_id}").fetchone()
        assert matched_present is not None
        assert orphan_present is not None
        return {
            "matched_row_wrongly_dropped": 0 if matched_present[0] > 0 else 1,
            "orphan_row_wrongly_kept": 0 if orphan_present[0] == 0 else 1,
        }

    assert_no_over_claims(oracle_con, _tables(s), sql, violations)
