"""Candidate keys from the ``ROW_NUMBER() ... = 1`` dedup idiom.

The ``QUALIFY ROW_NUMBER() OVER (PARTITION BY k) = 1`` pattern (and its subquery
twin, a projected ``ROW_NUMBER() AS rn`` filtered by an outer ``WHERE rn = 1``) keeps
one row per partition, so the partition columns are a candidate key of the output.
These pin that rule at the propagation boundary and its soundness edges: only
``ROW_NUMBER`` grounds it (``RANK`` / ``DENSE_RANK`` tie), only ``= 1`` / ``<= 1``
ground it, only a partition that resolves to output columns grounds it, and only a
window the query actually filters on grounds it.

The property enumerates the guard-form x rank-function x comparator space over a
random partition subset: the sound configurations derive exactly that subset as the
key, and every unsound variant derives nothing. The source carries no declared key,
so the rule under test is the only thing that can ground one.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given
from hypothesis import strategies as st

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_relation_graph
from dblect.lineage.properties.uniqueness import Key, uniqueness_property
from dblect.lineage.property import propagate
from dblect.manifest import Manifest, Node, ResourceType

_DUCKDB = profile_for_adapter("duckdb")

_RAW = "source.test.raw.events"
_MODEL = "model.test.deduped"
_POOL = ("c0", "c1", "c2")  # every model projects all three, so a partition subset maps


def _source() -> Node:
    return Node(
        unique_id=_RAW,
        name="events",
        resource_type=ResourceType.SOURCE,
        fqn=("test", "events"),
        package_name="test",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _model(sql: str) -> Node:
    return Node(
        unique_id=_MODEL,
        name="deduped",
        resource_type=ResourceType.MODEL,
        fqn=("test", "deduped"),
        package_name="test",
        schema="analytics",
        raw_code=sql,
        compiled_code=sql,
        original_file_path=None,
        columns={},
        depends_on=frozenset({_RAW}),
    )


def _keys(sql: str) -> frozenset[Key]:
    """The model's inferred candidate keys. The source declares none, so any key here
    is one the SQL proves."""
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in (_source(), _model(sql))},
    )
    anns = propagate(build_relation_graph(manifest).graph, uniqueness_property(manifest, _DUCKDB))
    return next(ann.value.keys for ref, ann in anns.items() if ref.unique_id == _MODEL)


def _key(*cols: str) -> Key:
    return frozenset(cols)


# --- canonical forms --------------------------------------------------------------


def test_qualify_inline_grounds_the_partition_key() -> None:
    sql = (
        "SELECT c0, c1, c2 FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY c1 ORDER BY c0) = 1"
    )
    assert _keys(sql) == {_key("c1")}


def test_qualify_named_grounds_the_partition_key() -> None:
    sql = (
        "SELECT c0, c1, c2, ROW_NUMBER() OVER (PARTITION BY c1, c2 ORDER BY c0) AS rn "
        "FROM events QUALIFY rn = 1"
    )
    assert _keys(sql) == {_key("c1", "c2")}


def test_subquery_where_grounds_the_partition_key() -> None:
    sql = (
        "SELECT c0, c1, c2 FROM ("
        "SELECT c0, c1, c2, ROW_NUMBER() OVER (PARTITION BY c1 ORDER BY c0) AS rn FROM events"
        ") sub WHERE rn = 1"
    )
    assert _keys(sql) == {_key("c1")}


def test_subquery_where_qualified_alias_grounds_the_partition_key() -> None:
    sql = (
        "SELECT c0, c1, c2 FROM ("
        "SELECT c0, c1, c2, ROW_NUMBER() OVER (PARTITION BY c2 ORDER BY c0) AS rn FROM events"
        ") sub WHERE sub.rn = 1"
    )
    assert _keys(sql) == {_key("c2")}


def test_le_one_guard_grounds_the_partition_key() -> None:
    sql = "SELECT c0, c1, c2 FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY c1) <= 1"
    assert _keys(sql) == {_key("c1")}


def test_partition_key_renames_through_the_projection() -> None:
    """The key rests on output names: a partition column aliased out is keyed by its alias."""
    sql = (
        "SELECT c0, c1 AS grp, c2 FROM events "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY c1 ORDER BY c0) = 1"
    )
    assert _keys(sql) == {_key("grp")}


# --- soundness edges: no key ------------------------------------------------------


def test_rank_grounds_no_key() -> None:
    """Ties under RANK yield several rows at rank 1, so the partition is not a key."""
    sql = "SELECT c0, c1, c2 FROM events QUALIFY RANK() OVER (PARTITION BY c1 ORDER BY c0) = 1"
    assert _keys(sql) == frozenset()


def test_dense_rank_grounds_no_key() -> None:
    sql = (
        "SELECT c0, c1, c2 FROM events QUALIFY DENSE_RANK() OVER (PARTITION BY c1 ORDER BY c0) = 1"
    )
    assert _keys(sql) == frozenset()


def test_threshold_two_grounds_no_key() -> None:
    sql = "SELECT c0, c1, c2 FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY c1) = 2"
    assert _keys(sql) == frozenset()


def test_greater_than_one_grounds_no_key() -> None:
    """``> 1`` keeps every row past the first per partition, not a single one."""
    sql = "SELECT c0, c1, c2 FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY c1) > 1"
    assert _keys(sql) == frozenset()


def test_partitionless_row_number_grounds_no_key() -> None:
    """A partition-less ``ROW_NUMBER() = 1`` selects one row for the whole relation (the
    empty key). We skip that strictly-stronger statement rather than model it here."""
    sql = "SELECT c0, c1, c2 FROM events QUALIFY ROW_NUMBER() OVER (ORDER BY c0) = 1"
    assert _keys(sql) == frozenset()


def test_projected_but_unfiltered_row_number_grounds_no_key() -> None:
    """A ``ROW_NUMBER()`` computed but never filtered does not dedup anything."""
    sql = "SELECT c0, c1, c2, ROW_NUMBER() OVER (PARTITION BY c1 ORDER BY c0) AS rn FROM events"
    assert _keys(sql) == frozenset()


def test_partition_on_expression_grounds_no_key() -> None:
    """A partition on an expression, not a bare output column, has no nameable key."""
    sql = "SELECT c0, c1, c2 FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY c1 + c2) = 1"
    assert _keys(sql) == frozenset()


def test_partition_column_absent_from_output_grounds_no_key() -> None:
    """Dedup on ``c1`` but ``c1`` is not projected: the key cannot be named on the output."""
    sql = "SELECT c0, c2 FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY c1 ORDER BY c0) = 1"
    assert _keys(sql) == frozenset()


def test_plain_equality_on_a_non_window_column_grounds_no_key() -> None:
    """A ``WHERE status = 1`` on an ordinary column is not a dedup guard."""
    sql = "SELECT c0, c1, c2 FROM events WHERE c1 = 1"
    assert _keys(sql) == frozenset()


# --- property: the guard-form x rank-function x comparator space -------------------

_RANK_FNS = ("ROW_NUMBER", "RANK", "DENSE_RANK")
_FILTERED_FORMS = ("qualify_inline", "qualify_named", "subquery_where")
_COMPARATORS = ("= 1", "<= 1", "= 2", "> 1", "< 3", ">= 1")


@dataclass(frozen=True)
class _Config:
    rank_fn: str
    form: str  # qualify_inline | qualify_named | subquery_where | unfiltered
    comparator: str
    partition: tuple[str, ...]  # empty means partition-less

    @property
    def sound(self) -> bool:
        return (
            self.rank_fn == "ROW_NUMBER"
            and self.form in _FILTERED_FORMS
            and self.comparator in ("= 1", "<= 1")
            and len(self.partition) > 0
        )

    @property
    def expected(self) -> frozenset[Key]:
        return frozenset({frozenset(self.partition)}) if self.sound else frozenset()

    def sql(self) -> str:
        cols = ", ".join(_POOL)
        part = ", ".join(self.partition)
        over = f"OVER (PARTITION BY {part} ORDER BY c0)" if self.partition else "OVER (ORDER BY c0)"
        window = f"{self.rank_fn}() {over}"
        if self.form == "qualify_inline":
            return f"SELECT {cols} FROM events QUALIFY {window} {self.comparator}"
        if self.form == "qualify_named":
            return f"SELECT {cols}, {window} AS rn FROM events QUALIFY rn {self.comparator}"
        if self.form == "subquery_where":
            return (
                f"SELECT {cols} FROM (SELECT {cols}, {window} AS rn FROM events) sub "
                f"WHERE rn {self.comparator}"
            )
        # unfiltered: the window is projected but no clause filters it.
        return f"SELECT {cols}, {window} AS rn FROM events"


@st.composite
def _configs(draw: st.DrawFn) -> _Config:
    partition = tuple(
        sorted(draw(st.lists(st.sampled_from(_POOL), min_size=0, max_size=3, unique=True)))
    )
    return _Config(
        rank_fn=draw(st.sampled_from(_RANK_FNS)),
        form=draw(st.sampled_from((*_FILTERED_FORMS, "unfiltered"))),
        comparator=draw(st.sampled_from(_COMPARATORS)),
        partition=partition,
    )


@given(config=_configs())
def test_rownumber_key_derivation_matches_the_contract(config: _Config) -> None:
    """A ROW_NUMBER dedup on a non-empty partition, guarded by ``= 1`` / ``<= 1`` in any of
    the filtered forms, derives exactly the partition key; every other configuration derives
    nothing. The source declares no key, so the rule under test is the only key source."""
    assert _keys(config.sql()) == config.expected
