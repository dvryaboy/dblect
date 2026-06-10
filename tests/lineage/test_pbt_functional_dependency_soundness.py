"""Empirical soundness PBT for the functional-dependency property: the data judges.

The FD walk claims dependencies on a model's output from three sources: a declared
dependency carried through the relational algebra, a constancy minted by an
equality filter, and the group key determining every other output of a GROUP BY.
The soundness obligation is uniform: every claimed ``X -> y`` must hold on the
materialized result, meaning no two result rows agree on ``X`` and differ on ``y``.

So this test generates a small scenario (a base table whose data satisfies the
declared dependency when one is declared, and a model built from a random
projection/rename, an optional equality filter, and an optional GROUP BY), asks
the analyzer for the model's FD set, materializes everything in duckdb, and checks
each claimed dependency against the rows. Over-claims anywhere in the walk (a
rename that blurs columns, a filter wrongly treated as pinning, a group key minted
from the wrong columns) surface as a concrete two-row counterexample, with no walk
rule restated in the test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import duckdb
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.lineage.builder import build_relation_graph
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.functional_dependency import (
    FD,
    FDSet,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import Manifest, Node, ResourceType

_SRC = SourceRef(SourceKind.SOURCE, "source.test.raw.t")
_MODEL = SourceRef(SourceKind.MODEL, "model.test.m")
_COLS = ("g", "x", "y")


@dataclass(frozen=True)
class Scenario:
    rows: tuple[tuple[int, int, int], ...]  # (g, x, y) per row
    declared: bool  # ``g -> x`` declared, and the data honours it
    where: tuple[str, int] | None  # equality filter ``col = literal``
    group_cols: tuple[str, ...]  # non-empty means GROUP BY these input columns
    renames: Mapping[str, str]  # projected input column -> output name


@st.composite
def _scenario(draw: st.DrawFn) -> Scenario:
    declared = draw(st.booleans())
    # When ``g -> x`` is declared the generated data must satisfy it, so ``x`` is a
    # drawn function of ``g`` rather than independent noise.
    mapping = {g: draw(st.integers(min_value=0, max_value=2)) for g in range(3)}
    rows: list[tuple[int, int, int]] = []
    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        g = draw(st.integers(min_value=0, max_value=2))
        x = mapping[g] if declared else draw(st.integers(min_value=0, max_value=2))
        y = draw(st.integers(min_value=0, max_value=3))
        rows.append((g, x, y))

    where = None
    if draw(st.booleans()):
        where = (draw(st.sampled_from(_COLS)), draw(st.integers(min_value=0, max_value=3)))

    if draw(st.booleans()):
        group_cols = tuple(
            sorted(draw(st.sets(st.sampled_from(("g", "x")), min_size=1, max_size=2)))
        )
        projected = group_cols
    else:
        group_cols = ()
        projected = tuple(sorted(draw(st.sets(st.sampled_from(_COLS), min_size=1, max_size=3))))
    names = draw(st.permutations(("a", "b", "c")))
    renames = {col: names[i] for i, col in enumerate(projected)}
    return Scenario(
        rows=tuple(rows), declared=declared, where=where, group_cols=group_cols, renames=renames
    )


def _model_sql(s: Scenario) -> str:
    projections = [f"{col} AS {name}" for col, name in s.renames.items()]
    if s.group_cols:
        projections.append("SUM(y) AS s")
    sql = f"SELECT {', '.join(projections)} FROM t"
    if s.where is not None:
        sql += f" WHERE {s.where[0]} = {s.where[1]}"
    if s.group_cols:
        sql += f" GROUP BY {', '.join(s.group_cols)}"
    return sql


def _claimed(s: Scenario) -> FDSet:
    """The model's FD set, exactly as the relation property derives it."""
    nodes = {
        _SRC.unique_id: Node(
            unique_id=_SRC.unique_id,
            name="t",
            resource_type=ResourceType.SOURCE,
            fqn=(_SRC.unique_id,),
            package_name="test",
            schema="raw",
            raw_code=None,
            compiled_code=None,
            original_file_path=None,
            columns={},
        ),
        _MODEL.unique_id: Node(
            unique_id=_MODEL.unique_id,
            name="m",
            resource_type=ResourceType.MODEL,
            fqn=(_MODEL.unique_id,),
            package_name="test",
            schema="analytics",
            raw_code=None,
            compiled_code=_model_sql(s),
            original_file_path=None,
            columns={},
        ),
    }
    manifest = Manifest(schema_version="v12", adapter_type="duckdb", nodes=nodes)
    value = FDSet.of(FD(frozenset({"g"}), "x")) if s.declared else FDSet(frozenset())
    fact = Fact(scope=_SRC, value=value, provenance=Declared(DeclaredSource.USER_ASSERTED))
    prop = functional_dependency_property(functional_dependency_grounding({_SRC: (fact,)}))
    anns = propagate(build_relation_graph(manifest).graph, prop)
    return anns[_MODEL].value


def _materialize(s: Scenario) -> tuple[tuple[str, ...], list[tuple[object, ...]]]:
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE t (g INTEGER, x INTEGER, y INTEGER)")
        if s.rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?)", [list(r) for r in s.rows])
        cursor = con.execute(_model_sql(s))
        description = cursor.description
        assert description is not None
        names = tuple(str(d[0]).lower() for d in description)
        return names, [tuple(r) for r in cursor.fetchall()]
    finally:
        con.close()


def _fd_holds(
    fd: FD, names: tuple[str, ...], rows: list[tuple[object, ...]]
) -> tuple[object, ...] | None:
    """``None`` when the dependency holds; otherwise a witness determinant value
    whose rows disagree on the dependent."""
    index = {name: i for i, name in enumerate(names)}
    seen: dict[tuple[object, ...], object] = {}
    for row in rows:
        key = tuple(row[index[col]] for col in sorted(fd.determinant))
        dep = row[index[fd.dependent]]
        if key in seen and seen[key] != dep:
            return key
        seen[key] = dep
    return None


@given(_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_every_claimed_fd_holds_on_the_data(s: Scenario) -> None:
    claimed = _claimed(s)
    assert not claimed.is_bottom
    if s.group_cols:
        # Anti-vacuity: a GROUP BY always yields at least the group-key dependency,
        # so a walk that silently claims nothing cannot pass on silence alone.
        assert claimed.fds
    names, rows = _materialize(s)
    for fd in claimed.fds:
        assert {fd.dependent, *fd.determinant} <= set(names), (
            f"claimed FD names a column the result lacks: {fd} vs {names} for sql={_model_sql(s)!r}"
        )
        witness = _fd_holds(fd, names, rows)
        assert witness is None, (
            f"claimed FD {sorted(fd.determinant)} -> {fd.dependent} violated at "
            f"determinant value {witness} for sql={_model_sql(s)!r} rows={rows}"
        )
