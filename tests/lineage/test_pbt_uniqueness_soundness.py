"""Empirical soundness PBT for uniqueness: the oracle is execution, not re-derivation.

The analytic uniqueness PBT (``test_pbt_uniqueness.py``) restates each rule and
asserts the propagator agrees; the scenario tests pin specific shapes. Neither
gives a shape-independent, ground-truth guarantee of the one invariant that must
never break: **a promoted candidate key is genuinely unique over real rows.**

This test closes that gap. It generates a small dbt-shaped scenario (sources with
``unique`` declarations plus one model that filters, joins, groups, or
de-duplicates), generates row data, runs the analyzer to get the model's promoted
keys, then materializes the model against the data in duckdb and asserts every
promoted key has no duplicate tuples. The oracle is the data, so unsoundness in
the join, group-by, distinct, or filter rules surfaces uniformly and for free as
new shapes are added, with no rule restated in the test.

This guards false positives (over-claiming a key), the soundness invariant.
Completeness (finding the keys we should) stays the job of the analytic and
scenario tests. The generator stays inside a grammar we control so the SQL always
executes, following the valid-SQL discipline of ``test_pbt_lineage.py``.

Scope of this first cut: single model over one or two sources, ``unique``
single-column declarations, and the filter / inner-join / left-join / group-by /
distinct shapes. Source data is generated non-null, so a declared-``unique``
column is a genuine key (the ``unique``-with-nulls question, where dbt's test
permits repeated nulls, is a separate axis left for a later extension). Conditional
(``where``-filtered) declarations and their activation are the next extension.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import duckdb
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.lineage.builder import build_relation_graph
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.uniqueness import Key, uniqueness_property
from dblect.lineage.property import propagate
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType

# Two sources, each a key column (declared unique) plus two plain columns. The
# plain columns range over a small domain so joins both match and miss and the
# joined side can carry duplicates on a non-key column.
_KEY_DOMAIN = 64  # distinct key values to draw from; large enough to stay unique
_PLAIN_DOMAIN = 4  # small, to force join matches, misses, and duplicates


@dataclass(frozen=True)
class SourceSpec:
    name: str
    key_col: str  # declared unique, generated distinct + non-null
    plain_cols: tuple[str, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        return (self.key_col, *self.plain_cols)


@dataclass(frozen=True)
class ModelSpec:
    """One generated model. ``shape`` selects the SQL form; the remaining fields
    carry the choices that shape needs (a join column, a group-by set, etc.)."""

    shape: str  # filter | inner_join | left_join | group_by | distinct
    select_cols: tuple[str, ...]  # output columns (bare, unqualified across sources)
    # join shapes:
    left_join_col: str | None = None
    right_join_col: str | None = None
    # filter shape:
    filter_col: str | None = None
    filter_threshold: int | None = None
    # group_by / distinct shapes:
    group_cols: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Scenario:
    sources: tuple[SourceSpec, ...]
    model: ModelSpec
    # Row data per source name, each row a tuple aligned with source.columns.
    data: tuple[tuple[str, tuple[tuple[int, ...], ...]], ...]


_S0 = SourceSpec(name="s0", key_col="k0", plain_cols=("a0", "b0"))
_S1 = SourceSpec(name="s1", key_col="k1", plain_cols=("a1", "b1"))


@st.composite
def _rows(draw: st.DrawFn, source: SourceSpec) -> tuple[tuple[int, ...], ...]:
    """Generate rows for a source: the key column is a distinct non-null int (so the
    ``unique`` declaration is a true key), every other column a small-domain int."""
    n = draw(st.integers(min_value=0, max_value=8))
    keys = draw(
        st.lists(
            st.integers(min_value=0, max_value=_KEY_DOMAIN - 1),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    rows: list[tuple[int, ...]] = []
    for key in keys:
        plain = tuple(
            draw(st.integers(min_value=0, max_value=_PLAIN_DOMAIN - 1)) for _ in source.plain_cols
        )
        rows.append((key, *plain))
    return tuple(rows)


@st.composite
def _scenario(draw: st.DrawFn) -> Scenario:
    shape = draw(st.sampled_from(("filter", "inner_join", "left_join", "group_by", "distinct")))
    is_join = shape in ("inner_join", "left_join")
    sources = (_S0, _S1) if is_join else (_S0,)

    if is_join:
        # Join an s0 column to an s1 column. Coverage (and thus key survival) holds
        # only when the right side is its key; the generator picks freely so the
        # analyzer's coverage logic is what is under test, and the data is the judge.
        left_join_col = draw(st.sampled_from(_S0.columns))
        right_join_col = draw(st.sampled_from(_S1.columns))
        model = ModelSpec(
            shape=shape,
            select_cols=("k0", "a1"),
            left_join_col=left_join_col,
            right_join_col=right_join_col,
        )
    elif shape == "filter":
        model = ModelSpec(
            shape=shape,
            select_cols=("k0", "a0"),
            filter_col=draw(st.sampled_from(_S0.columns)),
            filter_threshold=draw(st.integers(min_value=0, max_value=_PLAIN_DOMAIN)),
        )
    elif shape == "group_by":
        group_cols = tuple(
            sorted(draw(st.lists(st.sampled_from(_S0.columns), min_size=1, max_size=3, unique=True)))
        )
        model = ModelSpec(shape=shape, select_cols=(*group_cols, "n"), group_cols=group_cols)
    else:  # distinct
        cols = tuple(
            sorted(draw(st.lists(st.sampled_from(_S0.columns), min_size=1, max_size=3, unique=True)))
        )
        model = ModelSpec(shape=shape, select_cols=cols, group_cols=cols)

    data = tuple((s.name, draw(_rows(s))) for s in sources)
    return Scenario(sources=sources, model=model, data=data)


def _model_sql(m: ModelSpec) -> str:
    if m.shape in ("inner_join", "left_join"):
        join = "INNER JOIN" if m.shape == "inner_join" else "LEFT JOIN"
        return (
            f"SELECT s0.k0 AS k0, s1.a1 AS a1 "
            f"FROM s0 {join} s1 ON s0.{m.left_join_col} = s1.{m.right_join_col}"
        )
    if m.shape == "filter":
        return f"SELECT k0, a0 FROM s0 WHERE {m.filter_col} >= {m.filter_threshold}"
    if m.shape == "group_by":
        assert m.group_cols is not None
        cols = ", ".join(m.group_cols)
        return f"SELECT {cols}, COUNT(*) AS n FROM s0 GROUP BY {cols}"
    # distinct
    assert m.group_cols is not None
    return f"SELECT DISTINCT {', '.join(m.group_cols)} FROM s0"


def _source_node(s: SourceSpec) -> Node:
    return Node(
        unique_id=f"source.test.raw.{s.name}",
        name=s.name,
        resource_type=ResourceType.SOURCE,
        fqn=("test", s.name),
        package_name="test",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _unique_test(s: SourceSpec) -> Node:
    target = f"source.test.raw.{s.name}"
    return Node(
        unique_id=f"test.test.{s.name}_{s.key_col}_unique",
        name=f"{s.name}_{s.key_col}_unique",
        resource_type=ResourceType.OTHER,
        fqn=("test", f"{s.name}_{s.key_col}_unique"),
        package_name="test",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": s.key_col}),
        attached_node=target,
    )


def _model_node(s: Scenario) -> Node:
    sql = _model_sql(s.model)
    return Node(
        unique_id="model.test.m",
        name="m",
        resource_type=ResourceType.MODEL,
        fqn=("test", "m"),
        package_name="test",
        schema="analytics",
        raw_code=sql,
        compiled_code=sql,
        original_file_path=None,
        columns={},
        depends_on=frozenset(f"source.test.raw.{src.name}" for src in s.sources),
    )


def _manifest(s: Scenario) -> Manifest:
    nodes: list[Node] = []
    for src in s.sources:
        nodes.append(_source_node(src))
        nodes.append(_unique_test(src))
    nodes.append(_model_node(s))
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def _promoted_keys(s: Scenario) -> frozenset[Key]:
    """The model's promoted candidate keys, exactly as the analyzer derives them."""
    manifest = _manifest(s)
    graph = build_relation_graph(manifest).graph
    anns = propagate(graph, uniqueness_property(manifest))
    model_ref = SourceRef(SourceKind.MODEL, "model.test.m")
    return anns[model_ref].value.keys


def _materialize_counts(s: Scenario, keys: Sequence[Key]) -> tuple[int, dict[frozenset[str], int]]:
    """Create the sources in duckdb, materialize the model, and return its total row
    count plus, per key, the count of distinct key tuples."""
    con = duckdb.connect(":memory:")
    try:
        for src in s.sources:
            cols_ddl = ", ".join(f"{c} INTEGER" for c in src.columns)
            con.execute(f"CREATE TABLE {src.name} ({cols_ddl})")
        for name, rows in s.data:
            if not rows:
                continue
            placeholders = ", ".join(["?"] * len(rows[0]))
            con.executemany(f"INSERT INTO {name} VALUES ({placeholders})", [list(r) for r in rows])
        con.execute(f"CREATE TABLE _m AS {_model_sql(s.model)}")
        total = con.execute("SELECT COUNT(*) FROM _m").fetchone()
        assert total is not None
        distinct_counts: dict[frozenset[str], int] = {}
        for key in keys:
            cols = ", ".join(sorted(key))
            row = con.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM _m)").fetchone()
            assert row is not None
            distinct_counts[key] = row[0]
        return total[0], distinct_counts
    finally:
        con.close()


@given(_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_promoted_keys_are_truly_unique_over_materialized_rows(s: Scenario) -> None:
    """Every key the analyzer promotes for the model is genuinely unique over the
    rows duckdb materializes from the generated data.

    The test never recomputes which keys *should* survive; it takes whatever the
    analyzer promotes and checks it against the data. A key whose columns repeat a
    tuple in the materialized output is an unsound promotion (a false positive),
    which is the invariant this guards.
    """
    keys = _promoted_keys(s)
    output_cols = {c.lower() for c in s.model.select_cols}
    for key in keys:
        assert key <= output_cols, (
            f"promoted key {sorted(key)} is not a subset of output columns "
            f"{sorted(output_cols)} for sql={_model_sql(s.model)!r}"
        )

    total, distinct_counts = _materialize_counts(s, list(keys))
    for key, distinct in distinct_counts.items():
        assert distinct == total, (
            f"unsound key {sorted(key)}: {total} rows but {distinct} distinct key tuples "
            f"for sql={_model_sql(s.model)!r} data={s.data!r}"
        )
