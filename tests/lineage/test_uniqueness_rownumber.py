"""Candidate keys from the ``ROW_NUMBER() ... = 1`` dedup idiom.

The ``QUALIFY ROW_NUMBER() OVER (PARTITION BY k) = 1`` pattern (and its subquery
twin, a projected ``ROW_NUMBER() AS rn`` filtered by an outer ``WHERE rn = 1``) keeps
one row per partition, so the partition columns are a candidate key of the output.

``test_rownumber_key_derivation_matches_the_contract`` enumerates the closed decision
space (rank-function x guard-form x comparator x partition shape): the sound
configurations derive exactly the partition subset, every unsound variant derives
nothing. The standalone tests pin dimensions outside that grid (projection renaming, a
qualified alias, an expression partition, a partition column absent from the output, an
equality on an ordinary column) and the from-side/join boundary. Every model's source
declares no key, so the rule under test is the only thing that can ground one.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import pytest

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_relation_graph
from dblect.lineage.properties.uniqueness import Key, uniqueness_property
from dblect.lineage.property import propagate
from dblect.manifest import Manifest, Node, ResourceType

_DUCKDB = profile_for_adapter("duckdb")

_RAW = "source.test.raw.events"
_MODEL = "model.test.deduped"
_POOL = ("c0", "c1", "c2")  # every model projects all three, so a partition subset maps


def _source(unique_id: str = _RAW, name: str = "events") -> Node:
    return Node(
        unique_id=unique_id,
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


def _model(sql: str, *, deps: frozenset[str]) -> Node:
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
        depends_on=deps,
    )


def _keys(sql: str, *extra_sources: Node) -> frozenset[Key]:
    """The model's inferred candidate keys. The sources declare none, so any key here is one
    the SQL proves. Extra sources model additional FROM/JOIN relations (each keyless)."""
    model = _model(sql, deps=frozenset({_RAW, *(s.unique_id for s in extra_sources)}))
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in (_source(), *extra_sources, model)},
    )
    anns = propagate(build_relation_graph(manifest).graph, uniqueness_property(manifest, _DUCKDB))
    return next(ann.value.keys for ref, ann in anns.items() if ref.unique_id == _MODEL)


def _key(*cols: str) -> Key:
    return frozenset(cols)


# --- dimensions outside the enumerated grid ---------------------------------------


def test_subquery_where_qualified_alias_grounds_the_partition_key() -> None:
    """The outer guard may qualify the alias (``sub.rn``), not only bare ``rn``."""
    sql = (
        "SELECT c0, c1, c2 FROM ("
        "SELECT c0, c1, c2, ROW_NUMBER() OVER (PARTITION BY c2 ORDER BY c0) AS rn FROM events"
        ") sub WHERE sub.rn = 1"
    )
    assert _keys(sql) == {_key("c2")}


def test_partition_key_renames_through_the_projection() -> None:
    """The key rests on output names: a partition column aliased out is keyed by its alias."""
    sql = (
        "SELECT c0, c1 AS grp, c2 FROM events "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY c1 ORDER BY c0) = 1"
    )
    assert _keys(sql) == {_key("grp")}


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


def test_subquery_dedup_key_drops_across_a_fanning_join() -> None:
    """The subquery window is computed before the outer join, so the outer ``WHERE rn = 1``
    dedups the subquery yet a fanning join to a keyless target re-multiplies it. The partition
    is a key of the subquery, not of the joined output, so the derived key must ride through
    join preservation rather than bypass it: no key holds here. The inline-QUALIFY twin, whose
    window sees the post-join rows, stays keyed and is covered by the enumeration."""
    sql = (
        "SELECT sub.c1 AS c1, t.x AS x FROM ("
        "SELECT c0, c1, ROW_NUMBER() OVER (PARTITION BY c1 ORDER BY c0) AS rn FROM events"
        ") sub JOIN other t ON sub.c1 = t.c1 WHERE sub.rn = 1"
    )
    assert _keys(sql, _source("source.test.raw.other", "other")) == frozenset()


# --- enumeration: rank-function x guard-form x comparator x partition shape --------

_RANK_FNS = ("ROW_NUMBER", "RANK", "DENSE_RANK")
_FILTERED_FORMS = ("qualify_inline", "qualify_named", "subquery_where")
_FORMS = (*_FILTERED_FORMS, "unfiltered")
_COMPARATORS = ("= 1", "<= 1", "= 2", "> 1", "< 3", ">= 1")
_PARTITIONS = ((), ("c1",), ("c1", "c2"))  # empty (whole-relation), single, multi


@dataclass(frozen=True)
class _Config:
    rank_fn: str
    form: str
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


_CONFIGS = [
    _Config(rank_fn=r, form=f, comparator=c, partition=p)
    for r, f, c, p in itertools.product(_RANK_FNS, _FORMS, _COMPARATORS, _PARTITIONS)
]


@pytest.mark.parametrize(
    "config", _CONFIGS, ids=lambda c: f"{c.rank_fn}-{c.form}{c.comparator}-p{len(c.partition)}"
)
def test_rownumber_key_derivation_matches_the_contract(config: _Config) -> None:
    """A ROW_NUMBER dedup on a non-empty partition, guarded by ``= 1`` / ``<= 1`` in any of the
    filtered forms, derives exactly the partition key; every other configuration derives
    nothing."""
    assert _keys(config.sql()) == config.expected
