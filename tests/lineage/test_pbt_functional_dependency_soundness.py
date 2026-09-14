"""Empirical soundness PBT for the functional-dependency property: the data judges.

The FD walk claims dependencies on a model's output from three sources: a declared
dependency carried through the relational algebra (a union of arms drawn from the
declaring relation included), a constancy minted by an equality filter, and the
group key determining every other output of a GROUP BY.
The soundness obligation is uniform: every claimed ``X -> y`` must hold on the
materialized result, meaning no two result rows agree on ``X`` and differ on ``y``.

So each test generates a small scenario (base tables whose data satisfies the
declared dependency when one is declared, and a model built from a random
projection/rename plus the shape under test), asks the analyzer for the model's FD
set, materializes everything in duckdb, and checks each claimed dependency against
the rows. Over-claims anywhere in the walk (a rename that blurs columns, a filter
wrongly treated as pinning, a union merge that forgets which declared column feeds
which output) surface as a concrete two-row counterexample, with no walk rule
restated in the test.
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
    determines,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.property import propagate
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source
from tests.lineage._group_spelling import GroupSpelling

_MODEL = SourceRef(SourceKind.MODEL, "model.test.m")

# A table to materialize: its column DDL and its rows.
_Table = tuple[str, tuple[tuple[int, ...], ...]]


def _declared_fact(scope: SourceRef, *fds: FD) -> Fact[FDSet, SourceRef]:
    return Fact(
        scope=scope, value=FDSet.of(*fds), provenance=Declared(DeclaredSource.USER_ASSERTED)
    )


def _claimed_fds(
    sql: str,
    sources: Mapping[SourceRef, str],
    facts: Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]],
) -> FDSet:
    """The model's FD set over ``sql``, exactly as the relation property derives it."""
    tables = [_source(ref.unique_id, name=name) for ref, name in sources.items()]
    m = _manifest(*tables, _node(_MODEL.unique_id, sql, name="m"))
    prop = functional_dependency_property(functional_dependency_grounding(facts))
    return propagate(build_relation_graph(m).graph, prop)[_MODEL].value


def _materialize(
    con: duckdb.DuckDBPyConnection, sql: str, tables: Mapping[str, _Table]
) -> tuple[tuple[str, ...], list[tuple[object, ...]]]:
    """Create ``tables``, run ``sql``, and return the result's lowercased column
    names and rows."""
    try:
        for name, (columns, rows) in tables.items():
            con.execute(f"CREATE OR REPLACE TABLE {name} ({columns})")
            if rows:
                marks = ", ".join("?" for _ in rows[0])
                con.executemany(f"INSERT INTO {name} VALUES ({marks})", [list(r) for r in rows])
        cursor = con.execute(sql)
        description = cursor.description
        assert description is not None
        names = tuple(str(d[0]).lower() for d in description)
        return names, [tuple(r) for r in cursor.fetchall()]
    finally:
        for name in tables:
            con.execute(f"DROP TABLE IF EXISTS {name}")


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


def _assert_claims_hold(
    claimed: FDSet, names: tuple[str, ...], rows: list[tuple[object, ...]], sql: str
) -> None:
    """Every claimed dependency judged against the materialized rows."""
    for fd in claimed.fds:
        assert {fd.dependent, *fd.determinant} <= set(names), (
            f"claimed FD names a column the result lacks: {fd} vs {names} for sql={sql!r}"
        )
        witness = _fd_holds(fd, names, rows)
        assert witness is None, (
            f"claimed FD {sorted(fd.determinant)} -> {fd.dependent} violated at "
            f"determinant value {witness} for sql={sql!r} rows={rows}"
        )


# --- projection, filter, and GROUP BY over one table -------------------------------

_SRC = SourceRef(SourceKind.SOURCE, "source.test.raw.t")
_COLS = ("g", "x", "y")


@dataclass(frozen=True)
class Scenario:
    rows: tuple[tuple[int, int, int], ...]  # (g, x, y) per row
    declared: bool  # ``g -> x`` declared, and the data honours it
    where: tuple[str, int] | None  # equality filter ``col = literal``
    group_cols: tuple[str, ...]  # non-empty means GROUP BY these input columns
    group_spelling: GroupSpelling  # how that GROUP BY names them
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
    # Every projection here renames, so an ordinal has to see through the AS binding to the
    # input column and then let the projection rename it back.
    spelling = draw(st.sampled_from((GroupSpelling.EXPRESSION, GroupSpelling.ORDINAL)))
    return Scenario(
        rows=tuple(rows),
        declared=declared,
        where=where,
        group_cols=group_cols,
        group_spelling=spelling,
        renames=renames,
    )


def _model_sql(s: Scenario) -> str:
    projections = [f"{col} AS {name}" for col, name in s.renames.items()]
    if s.group_cols:
        projections.append("SUM(y) AS s")
    sql = f"SELECT {', '.join(projections)} FROM t"
    if s.where is not None:
        sql += f" WHERE {s.where[0]} = {s.where[1]}"
    if s.group_cols:
        # The group columns lead the projection list, so ordinal i names group column i.
        targets = (
            ", ".join(str(i + 1) for i in range(len(s.group_cols)))
            if s.group_spelling is GroupSpelling.ORDINAL
            else ", ".join(s.group_cols)
        )
        sql += f" GROUP BY {targets}"
    return sql


@given(s=_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_every_claimed_fd_holds_on_the_data(
    oracle_con: duckdb.DuckDBPyConnection, s: Scenario
) -> None:
    fds = (FD(frozenset({"g"}), "x"),) if s.declared else ()
    claimed = _claimed_fds(_model_sql(s), {_SRC: "t"}, {_SRC: (_declared_fact(_SRC, *fds),)})
    assert not claimed.is_bottom
    if s.group_cols:
        # Anti-vacuity: a GROUP BY always yields at least the group-key dependency,
        # so a walk that silently claims nothing cannot pass on silence alone.
        assert claimed.fds
    names, rows = _materialize(
        oracle_con, _model_sql(s), {"t": ("g INTEGER, x INTEGER, y INTEGER", s.rows)}
    )
    _assert_claims_hold(claimed, names, rows, _model_sql(s))


# --- dependency through a join (C4) ----------------------------------------------
#
# A second base table is joined in, with the join side drawn alongside the data. An FD
# declared on a relation (its data honouring it) must still hold on the join wherever
# that relation is a kept side, whatever the join's fan-out does: duplicated rows still
# agree on the dependent. The data is again the judge, so a qualified-projection bug
# that minted the dependency off the wrong same-named column, or a kept/padded mixup,
# would surface as a two-row counterexample. The padded side's drop is pinned as the
# contract too: the walk must stay silent about a NULL-padded relation.

_PAY = SourceRef(SourceKind.SOURCE, "source.test.raw.pay")
_DIM = SourceRef(SourceKind.SOURCE, "source.test.raw.dim")
_QCOLS: tuple[tuple[str, str], ...] = (("p", "k"), ("p", "a"), ("d", "k"), ("d", "g"), ("d", "v"))
_JOIN_KINDS: Mapping[str, str] = {
    "inner": "JOIN dim d ON p.k = d.k",
    "left": "LEFT JOIN dim d ON p.k = d.k",
    "right": "RIGHT JOIN dim d ON p.k = d.k",
    "full": "FULL JOIN dim d ON p.k = d.k",
    "cross": "CROSS JOIN dim d",
}
# The sides whose rows come through un-padded, per join kind: their declared FDs
# must be claimed (anti-vacuity) and every claim must hold on the data.
_KEPT: Mapping[str, frozenset[str]] = {
    "inner": frozenset({"p", "d"}),
    "left": frozenset({"p"}),
    "right": frozenset({"d"}),
    "full": frozenset(),
    "cross": frozenset({"p", "d"}),
}


@dataclass(frozen=True)
class JoinScenario:
    side: str  # key into _JOIN_KINDS
    rows_pay: tuple[tuple[int, int], ...]  # (k, a), with a a function of k (k -> a holds)
    rows_dim: tuple[tuple[int, int, int], ...]  # (k, g, v), with v a function of g (g -> v holds)
    projection: tuple[tuple[tuple[str, str], str], ...]  # ((alias, column), output name)


@st.composite
def _join_scenario(draw: st.DrawFn) -> JoinScenario:
    side = draw(st.sampled_from(sorted(_JOIN_KINDS)))
    amap = {k: draw(st.integers(min_value=0, max_value=9)) for k in range(3)}
    vmap = {g: draw(st.integers(min_value=0, max_value=2)) for g in range(3)}
    rows_pay: list[tuple[int, int]] = []
    for _ in range(draw(st.integers(min_value=0, max_value=6))):
        k = draw(st.integers(min_value=0, max_value=2))
        rows_pay.append((k, amap[k]))  # a determined by k, so k -> a holds in the data
    rows_dim: list[tuple[int, int, int]] = []
    for _ in range(draw(st.integers(min_value=0, max_value=6))):
        k = draw(st.integers(min_value=0, max_value=2))
        g = draw(st.integers(min_value=0, max_value=2))
        rows_dim.append((k, g, vmap[g]))  # v determined by g, so g -> v holds in the data
    chosen = draw(st.lists(st.sampled_from(_QCOLS), min_size=1, max_size=5, unique=True))
    projection = tuple((qc, f"o{i}") for i, qc in enumerate(chosen))
    return JoinScenario(
        side=side, rows_pay=tuple(rows_pay), rows_dim=tuple(rows_dim), projection=projection
    )


def _join_sql(s: JoinScenario) -> str:
    cols = ", ".join(f"{alias}.{col} AS {name}" for (alias, col), name in s.projection)
    return f"SELECT {cols} FROM pay p {_JOIN_KINDS[s.side]}"


@given(s=_join_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_every_claimed_join_fd_holds_on_the_data(
    oracle_con: duckdb.DuckDBPyConnection, s: JoinScenario
) -> None:
    facts = {
        _PAY: (_declared_fact(_PAY, FD(frozenset({"k"}), "a")),),
        _DIM: (_declared_fact(_DIM, FD(frozenset({"g"}), "v")),),
    }
    claimed = _claimed_fds(_join_sql(s), {_PAY: "pay", _DIM: "dim"}, facts)
    assert not claimed.is_bottom
    selected = dict(s.projection)
    declared = {"p": (("p", "k"), ("p", "a")), "d": (("d", "g"), ("d", "v"))}
    for alias, (det, dep) in declared.items():
        if det not in selected or dep not in selected:
            continue
        out_fd = FD(frozenset({selected[det]}), selected[dep])
        if alias in _KEPT[s.side]:
            # Anti-vacuity: a kept side's declared dependency must be carried through
            # the join (a silent walk cannot pass on silence alone).
            assert out_fd in claimed.fds, f"kept-side FD dropped for sql={_join_sql(s)!r}"
        else:
            # The padded side's drop is the contract: NULL padding can break the
            # dependency, so the walk must stay silent about it.
            assert out_fd not in claimed.fds, f"padded-side FD claimed for sql={_join_sql(s)!r}"
    names, rows = _materialize(
        oracle_con,
        _join_sql(s),
        {
            "pay": ("k INTEGER, a INTEGER", s.rows_pay),
            "dim": ("k INTEGER, g INTEGER, v INTEGER", s.rows_dim),
        },
    )
    _assert_claims_hold(claimed, names, rows, _join_sql(s))


# --- declared dependencies across a union ------------------------------------------
#
# Two base tables each honour ``{g, h} -> x`` in their own data, under their own
# mapping. The walk may keep the dependency across a union only when every arm's
# pairs come from one declared world under one column binding. Each way the merge
# can go wrong is in the generated space: two worlds' mappings disagree, arms
# permuting the determinant columns run the axiom two different ways, and a BY
# NAME merge lines the arms up by alias rather than position. The materialized
# union judges all of it. The anti-vacuity arm pins the flip side: when both arms
# are slices of the one declared relation, projected identically and merged
# positionally, silence is a failure.

_T1 = SourceRef(SourceKind.SOURCE, "source.test.raw.t1")
_T2 = SourceRef(SourceKind.SOURCE, "source.test.raw.t2")
_GH_X = FD(frozenset({"g", "h"}), "x")


@dataclass(frozen=True)
class UnionScenario:
    same_world: bool  # arm two reads t1 (the declaring relation) rather than t2
    declare_t2: bool  # t2 carries its own ``{g, h} -> x`` declaration
    rows_t1: tuple[tuple[int, int, int], ...]  # (g, h, x), x a function of (g, h)
    rows_t2: tuple[tuple[int, int, int], ...]  # same shape, independent mapping
    arm1_cols: tuple[str, ...]  # first arm's projection order over (g, h, x)
    arm2_cols: tuple[str, ...]  # second arm's projection order (may permute)
    arm2_names: tuple[str, ...]  # second arm's aliases (a positional merge ignores them)
    distinct: bool  # UNION rather than UNION ALL
    by_name: bool  # merge BY NAME rather than positionally


@st.composite
def _union_scenario(draw: st.DrawFn) -> UnionScenario:
    def rows(mapping: Mapping[tuple[int, int], int]) -> tuple[tuple[int, int, int], ...]:
        out: list[tuple[int, int, int]] = []
        for _ in range(draw(st.integers(min_value=0, max_value=6))):
            g = draw(st.integers(min_value=0, max_value=1))
            h = draw(st.integers(min_value=0, max_value=1))
            out.append((g, h, mapping[(g, h)]))
        return tuple(out)

    def mapping() -> dict[tuple[int, int], int]:
        return {(g, h): draw(st.integers(min_value=0, max_value=2)) for g in (0, 1) for h in (0, 1)}

    map1, map2 = mapping(), mapping()
    perm = st.permutations(("g", "h", "x"))
    return UnionScenario(
        same_world=draw(st.booleans()),
        declare_t2=draw(st.booleans()),
        rows_t1=rows(map1),
        rows_t2=rows(map2),
        arm1_cols=tuple(draw(perm)),
        arm2_cols=tuple(draw(perm)),
        arm2_names=tuple(draw(st.one_of(perm, st.just(("p0", "p1", "p2"))))),
        distinct=draw(st.booleans()),
        by_name=draw(st.booleans()),
    )


def _union_sql(s: UnionScenario) -> str:
    arm1 = ", ".join(f"{col} AS o{i}" for i, col in enumerate(s.arm1_cols))
    arm2 = ", ".join(
        f"{col} AS {name}" for col, name in zip(s.arm2_cols, s.arm2_names, strict=True)
    )
    arm2_table = "t1" if s.same_world else "t2"
    op = ("UNION" if s.distinct else "UNION ALL") + (" BY NAME" if s.by_name else "")
    return f"SELECT {arm1} FROM t1 {op} SELECT {arm2} FROM {arm2_table}"


@given(s=_union_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_every_claimed_union_fd_holds_on_the_data(
    oracle_con: duckdb.DuckDBPyConnection, s: UnionScenario
) -> None:
    facts = {_T1: (_declared_fact(_T1, _GH_X),)}
    if s.declare_t2:
        facts[_T2] = (_declared_fact(_T2, _GH_X),)
    claimed = _claimed_fds(_union_sql(s), {_T1: "t1", _T2: "t2"}, facts)
    assert not claimed.is_bottom
    if s.same_world and s.arm1_cols == s.arm2_cols and not s.by_name:
        # Anti-vacuity: both arms are the declared relation projected identically,
        # so the merge must keep the declared dependency (silence cannot pass).
        out = {col: f"o{i}" for i, col in enumerate(s.arm1_cols)}
        assert determines(claimed, frozenset({out["g"], out["h"]}), out["x"]), (
            f"same-world union dropped the declared FD for sql={_union_sql(s)!r}"
        )
    ddl = "g INTEGER, h INTEGER, x INTEGER"
    names, rows = _materialize(
        oracle_con, _union_sql(s), {"t1": (ddl, s.rows_t1), "t2": (ddl, s.rows_t2)}
    )
    _assert_claims_hold(claimed, names, rows, _union_sql(s))
