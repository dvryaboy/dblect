"""Empirical soundness PBT for uniqueness: the oracle is execution, not re-derivation.

The analytic uniqueness PBT (``test_pbt_uniqueness.py``) restates each rule and
asserts the propagator agrees; the scenario tests pin specific shapes. Neither
gives a shape-independent, ground-truth guarantee of the one invariant that must
never break: **a promoted candidate key is genuinely unique over real rows.**

This test closes that gap. It generates a small dbt-shaped scenario, runs the
analyzer to get the model's promoted keys (with conditional activation, exactly as
the audit path derives them), then materializes the model against generated data in
duckdb and asserts every promoted key has no duplicate tuples. The oracle is the
data, so unsoundness in the join, group-by, distinct, filter, or conditional
activation rules surfaces uniformly and for free as new shapes are added, with no
rule restated in the test.

Two generators feed one soundness checker:

* **Unconditional shapes** (``_scenario``): one model over one or two sources with
  ``unique`` declarations, in the filter / inner-join / left-join / group-by /
  distinct forms. The group-by form draws how it names its targets (see
  ``GroupSpelling``), so the ordinals a dbt project actually writes are judged by the
  same oracle as the spelled-out form.
* **Conditional activation** (``_cond_scenario``): a ``where``-filtered ``unique``
  on a source whose downstream model applies a filter that may or may not imply the
  predicate. Source data honors the conditional declaration (``id`` is distinct only
  within the predicate subset), so if activation promotes the key when the filter
  does not actually restrict to that subset, the materialized rows carry a duplicate
  and the check fails.

This guards false positives (over-claiming a key), the soundness invariant.
Completeness (finding the keys we should) stays the job of the analytic and scenario
tests. Generators stay inside a grammar we control so the SQL always executes,
following the valid-SQL discipline of ``test_pbt_lineage.py``. Source data is
non-null, so a declared-``unique`` column is a genuine key (the ``unique``-with-nulls
question, where dbt's test permits repeated nulls, is a separate axis left for
later). Multi-model chains are the next extension.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

import duckdb
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_relation_graph
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.predicate_flow import predicate_flow_property
from dblect.lineage.properties.uniqueness import (
    Key,
    activate_conditional,
    uniqueness_property,
)
from dblect.lineage.property import propagate
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests.lineage._duckdb_oracle import Table, materialized, scalar
from tests.lineage._group_spelling import GroupSpelling

_DUCKDB = profile_for_adapter("duckdb")

_MODEL_UID = "model.test.m"

# --- shared analyzer + duckdb oracle ----------------------------------------------


def _promoted_keys(manifest: Manifest) -> frozenset[Key]:
    """The model's promoted candidate keys, exactly as the audit path derives them:
    propagate uniqueness and predicate-flow, then activate conditional keys."""
    graph = build_relation_graph(manifest).graph
    keys = propagate(graph, uniqueness_property(manifest, _DUCKDB))
    flow = propagate(graph, predicate_flow_property())
    activated = activate_conditional(keys, flow)
    return activated[SourceRef(SourceKind.MODEL, _MODEL_UID)].keys


def _assert_keys_sound(
    con: duckdb.DuckDBPyConnection,
    tables: Sequence[Table],
    model_sql: str,
    keys: frozenset[Key],
) -> None:
    """Materialize ``tables`` and the model in duckdb; assert every key in ``keys`` has
    as many distinct key tuples as the model has rows (so it is genuinely unique)."""
    with materialized(con, tables, model_sql) as con:
        total = scalar(con, "SELECT COUNT(*) FROM _m")
        for key in keys:
            cols = ", ".join(sorted(key))
            distinct = scalar(con, f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM _m)")
            assert distinct == total, (
                f"unsound key {sorted(key)}: {total} rows but {distinct} distinct tuples "
                f"for sql={model_sql!r} tables={tables!r}"
            )


def _source_node(name: str, schema: str = "raw") -> Node:
    return _node(f"source.test.{schema}.{name}", kind=ResourceType.SOURCE, schema=schema)


def _unique_test(source_name: str, *, column: str, where: str | None = None) -> Node:
    target = f"source.test.raw.{source_name}"
    suffix = "_cond" if where is not None else ""
    return _node(
        f"test.test.{source_name}_{column}_unique{suffix}",
        kind=ResourceType.OTHER,
        name=f"{source_name}_{column}_unique{suffix}",
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}, where=where),
        attached_node=target,
    )


def _unique_combination_test(source_name: str, *, columns: tuple[str, str]) -> Node:
    target = f"source.test.raw.{source_name}"
    suffix = "_".join(columns)
    return _node(
        f"test.test.{source_name}_{suffix}_unique_combo",
        kind=ResourceType.OTHER,
        name=f"{source_name}_{suffix}_unique_combo",
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(
            name="dbt_utils.unique_combination_of_columns",
            kwargs={"combination_of_columns": list(columns)},
        ),
        attached_node=target,
    )


def _model_node(sql: str, *, depends_on: frozenset[str]) -> Node:
    return _node(_MODEL_UID, sql, raw=sql, name="m", depends_on=depends_on)


# --- unconditional shapes ---------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    name: str
    key_col: str  # declared unique, generated distinct + non-null
    plain_cols: tuple[str, ...]
    # When set, the source declares unique_combination_of_columns(key_col, composite_with)
    # instead of unique(key_col); rows are then distinct on that pair alone.
    composite_with: str | None = None

    @property
    def columns(self) -> tuple[str, ...]:
        return (self.key_col, *self.plain_cols)


@dataclass(frozen=True)
class ModelSpec:
    shape: str  # filter | inner_join | left_join | group_by | distinct | qualify
    # | anti/semi shapes | join_grouped
    select_cols: tuple[str, ...]
    left_join_col: str | None = None
    right_join_col: str | None = None
    # An inner join may project the joined-in side's own join column, aliased under
    # the probe's usual output name, instead of the probe's column: the shape where
    # the current walk loses the key.
    project_other_side: bool = False
    filter_col: str | None = None
    filter_threshold: int | None = None
    filter_op: str = ">="
    group_cols: tuple[str, ...] | None = None
    group_spelling: GroupSpelling | None = None
    partition_cols: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Scenario:
    sources: tuple[SourceSpec, ...]
    model: ModelSpec
    data: tuple[tuple[str, tuple[tuple[int, ...], ...]], ...]


_S0 = SourceSpec(name="s0", key_col="k0", plain_cols=("a0", "b0"))
_S1 = SourceSpec(name="s1", key_col="k1", plain_cols=("a1", "b1"))
_KEY_DOMAIN = 64
_PLAIN_DOMAIN = 4
_COMPOSITE_DOMAIN = 4  # small enough that each column repeats while the pair stays unique


@st.composite
def _rows(draw: st.DrawFn, source: SourceSpec) -> tuple[tuple[int, ...], ...]:
    """Rows for a source. With no composite key, the key column is distinct non-null
    (so ``unique`` is a true key), every other column a small-domain int (to force
    join matches and duplicates). With a composite key, the (key_col, composite_with)
    pair is distinct as a tuple while each of the two columns is free to repeat alone,
    so ``unique_combination_of_columns`` is a true key but neither column is by itself."""
    n = draw(st.integers(min_value=0, max_value=8))
    if source.composite_with is None:
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
                draw(st.integers(min_value=0, max_value=_PLAIN_DOMAIN - 1))
                for _ in source.plain_cols
            )
            rows.append((key, *plain))
        return tuple(rows)

    extra_index = source.plain_cols.index(source.composite_with)
    pairs = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=_COMPOSITE_DOMAIN - 1),
                st.integers(min_value=0, max_value=_COMPOSITE_DOMAIN - 1),
            ),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    composite_rows: list[tuple[int, ...]] = []
    for key, extra in pairs:
        plain = tuple(
            extra
            if i == extra_index
            else draw(st.integers(min_value=0, max_value=_PLAIN_DOMAIN - 1))
            for i in range(len(source.plain_cols))
        )
        composite_rows.append((key, *plain))
    return tuple(composite_rows)


def _column_subset(draw: st.DrawFn) -> tuple[str, ...]:
    """A sorted distinct subset of ``s0``'s columns, the shape GROUP BY and DISTINCT share."""
    return tuple(
        sorted(draw(st.lists(st.sampled_from(_S0.columns), min_size=1, max_size=3, unique=True)))
    )


@st.composite
def _scenario(draw: st.DrawFn) -> Scenario:
    shape = draw(
        st.sampled_from(
            (
                "filter",
                "inner_join",
                "left_join",
                "group_by",
                "distinct",
                "qualify",
                "anti_join",
                "semi_join",
                "left_is_null",
                "join_grouped",
            )
        )
    )
    # The anti/semi shapes filter s0 by s1, so they project s0 alone but still need s1's data.
    anti_shapes = ("anti_join", "semi_join", "left_is_null")
    is_join = shape in ("inner_join", "left_join")
    sources = (_S0, _S1) if is_join or shape in anti_shapes else (_S0,)

    # s0 always participates, so it carries the choice of a composite declared key
    # (unique_combination_of_columns over k0 and a second column) instead of a plain
    # unique(k0); the pair is distinct while either column alone is free to repeat.
    if draw(st.booleans()):
        second = draw(st.sampled_from(_S0.plain_cols))
        sources = tuple(
            replace(src, composite_with=second) if src.name == _S0.name else src for src in sources
        )

    if shape == "join_grouped":
        # A self-join back to s0 grouped by one column with MAX() of another; the
        # walk derives no coarse key here, so the oracle must still see none promoted.
        gcol, other = draw(
            st.lists(st.sampled_from(_S0.columns), min_size=2, max_size=2, unique=True)
        )
        model = ModelSpec(shape=shape, select_cols=_S0.columns, group_cols=(gcol, other))
    elif shape in anti_shapes:
        # An anti/semi filter preserves s0's declared key {k0}; the oracle proves it over rows.
        model = ModelSpec(
            shape=shape,
            select_cols=("k0", "a0"),
            left_join_col=draw(st.sampled_from(_S0.columns)),
            right_join_col=draw(st.sampled_from(_S1.columns)),
        )
    elif shape == "qualify":
        # Dedup s0 to one row per partition subset; the derived key is that subset.
        part = tuple(
            sorted(
                draw(st.lists(st.sampled_from(_S0.columns), min_size=1, max_size=3, unique=True))
            )
        )
        model = ModelSpec(shape=shape, select_cols=_S0.columns, partition_cols=part)
    elif is_join:
        # An inner join may project s1's own join column, aliased as ``k0``, instead of
        # s0's: the ON equates them, so the oracle must still see no unsound key.
        project_other_side = shape == "inner_join" and draw(st.booleans())
        model = ModelSpec(
            shape=shape,
            select_cols=("k0", "a1"),
            left_join_col="k0" if project_other_side else draw(st.sampled_from(_S0.columns)),
            right_join_col=draw(st.sampled_from(_S1.columns)),
            project_other_side=project_other_side,
        )
    elif shape == "filter":
        model = ModelSpec(
            shape=shape,
            select_cols=("k0", "a0"),
            filter_col=draw(st.sampled_from(_S0.columns)),
            filter_threshold=draw(st.integers(min_value=0, max_value=_PLAIN_DOMAIN)),
            filter_op=draw(st.sampled_from((">=", "="))),
        )
    elif shape == "group_by":
        spelling = draw(st.sampled_from(tuple(GroupSpelling)))
        if spelling is GroupSpelling.SHADOWING_ALIAS:
            # ``SELECT source AS name ... GROUP BY name, source``: the group key is
            # (input ``name``, ``source``) while the output carries only ``source`` under
            # ``name``'s spelling, so two groups can share an output value.
            name_col, source_col = draw(
                st.lists(st.sampled_from(_S0.columns), min_size=2, max_size=2, unique=True)
            )
            model = ModelSpec(
                shape=shape,
                select_cols=(name_col, "n"),
                group_cols=(name_col, source_col),
                group_spelling=spelling,
            )
        else:
            cols = _column_subset(draw)
            model = ModelSpec(
                shape=shape, select_cols=(*cols, "n"), group_cols=cols, group_spelling=spelling
            )
    else:  # distinct
        cols = _column_subset(draw)
        model = ModelSpec(shape=shape, select_cols=cols, group_cols=cols)

    data = tuple((s.name, draw(_rows(s))) for s in sources)
    return Scenario(sources=sources, model=model, data=data)


def _scenario_sql(m: ModelSpec) -> str:
    if m.shape in ("inner_join", "left_join"):
        join = "INNER JOIN" if m.shape == "inner_join" else "LEFT JOIN"
        first = f"s1.{m.right_join_col} AS k0" if m.project_other_side else "s0.k0 AS k0"
        return (
            f"SELECT {first}, s1.a1 AS a1 "
            f"FROM s0 {join} s1 ON s0.{m.left_join_col} = s1.{m.right_join_col}"
        )
    if m.shape == "join_grouped":
        assert m.group_cols is not None
        gcol, other = m.group_cols
        cols = ", ".join(f"l.{c} AS {c}" for c in m.select_cols)
        return (
            f"SELECT {cols} FROM s0 l JOIN "
            f"(SELECT {gcol}, MAX({other}) AS {other} FROM s0 GROUP BY {gcol}) m "
            f"ON l.{gcol} = m.{gcol} AND l.{other} = m.{other}"
        )
    if m.shape in ("anti_join", "semi_join", "left_is_null"):
        base = "SELECT s0.k0 AS k0, s0.a0 AS a0 FROM s0"
        on = f"ON s0.{m.left_join_col} = s1.{m.right_join_col}"
        if m.shape == "anti_join":
            return f"{base} ANTI JOIN s1 {on}"
        if m.shape == "semi_join":
            return f"{base} SEMI JOIN s1 {on}"
        # left_is_null: the IS NULL sits on s1's join-key column, the recognised anti-join idiom.
        return f"{base} LEFT JOIN s1 {on} WHERE s1.{m.right_join_col} IS NULL"
    if m.shape == "filter":
        return f"SELECT k0, a0 FROM s0 WHERE {m.filter_col} {m.filter_op} {m.filter_threshold}"
    if m.shape == "qualify":
        assert m.partition_cols is not None
        cols = ", ".join(m.select_cols)
        part = ", ".join(m.partition_cols)
        return (
            f"SELECT {cols} FROM s0 QUALIFY ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY k0) = 1"
        )
    assert m.group_cols is not None
    cols = ", ".join(m.group_cols)
    if m.shape != "group_by":
        return f"SELECT DISTINCT {cols} FROM s0"
    if m.group_spelling is GroupSpelling.SHADOWING_ALIAS:
        name_col, source_col = m.group_cols
        return (
            f"SELECT {source_col} AS {name_col}, COUNT(*) AS n "
            f"FROM s0 GROUP BY {name_col}, {source_col}"
        )
    targets = (
        ", ".join(str(i + 1) for i in range(len(m.group_cols)))
        if m.group_spelling is GroupSpelling.ORDINAL
        else cols
    )
    return f"SELECT {cols}, COUNT(*) AS n FROM s0 GROUP BY {targets}"


def _scenario_manifest(s: Scenario) -> Manifest:
    nodes: list[Node] = []
    for src in s.sources:
        nodes.append(_source_node(src.name))
        if src.composite_with is not None:
            nodes.append(
                _unique_combination_test(src.name, columns=(src.key_col, src.composite_with))
            )
        else:
            nodes.append(_unique_test(src.name, column=src.key_col))
    nodes.append(
        _model_node(
            _scenario_sql(s.model),
            depends_on=frozenset(f"source.test.raw.{src.name}" for src in s.sources),
        )
    )
    return _manifest(*nodes)


@given(s=_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_unconditional_promoted_keys_are_unique_over_materialized_rows(
    oracle_con: duckdb.DuckDBPyConnection, s: Scenario
) -> None:
    """Every key the analyzer promotes for a filter/join/group/distinct model is
    genuinely unique over the duckdb-materialized rows. The test never recomputes
    which keys should survive; the data is the judge."""
    keys = _promoted_keys(_scenario_manifest(s))
    output_cols = {c.lower() for c in s.model.select_cols}
    for key in keys:
        assert key <= output_cols, f"key {sorted(key)} not in outputs {sorted(output_cols)}"
    data_by_name = dict(s.data)
    tables: list[Table] = [(src.name, src.columns, data_by_name[src.name]) for src in s.sources]
    _assert_keys_sound(oracle_con, tables, _scenario_sql(s.model), keys)


# --- conditional activation -------------------------------------------------------


@dataclass(frozen=True)
class CondScenario:
    """A ``where``-filtered ``unique(id) where g > test_threshold`` on the source, and a
    downstream model ``SELECT id, g FROM orders WHERE g > model_threshold``.

    ``rows`` honors the conditional declaration: ``id`` is distinct among rows where
    ``g > test_threshold`` (the predicate subset) and free to repeat elsewhere. So when
    the model filter implies the predicate (``model_threshold >= test_threshold``) the
    activated key is genuinely unique; when it does not, the output can carry duplicate
    ids, which the data catches if activation wrongly fires.
    """

    test_threshold: int
    model_threshold: int
    rows: tuple[tuple[int, int], ...]  # (id, g)


_G_MAX = 4
_ID_POOL = 10


@st.composite
def _cond_scenario(draw: st.DrawFn) -> CondScenario:
    b = draw(st.integers(min_value=0, max_value=_G_MAX))
    a = draw(st.integers(min_value=0, max_value=_G_MAX))

    high_g_choices = list(range(b + 1, _G_MAX + 1))  # g values that satisfy g > b
    n_high = draw(st.integers(min_value=0, max_value=5)) if high_g_choices else 0
    high_ids = draw(
        st.lists(
            st.integers(min_value=0, max_value=_ID_POOL - 1),
            min_size=n_high,
            max_size=n_high,
            unique=True,
        )
    )
    rows: list[tuple[int, int]] = [
        (i, draw(st.sampled_from(high_g_choices))) for i in high_ids
    ]  # distinct ids within the predicate subset

    n_low = draw(st.integers(min_value=0, max_value=5))
    rows.extend(  # g <= b, ids unconstrained (may repeat)
        (draw(st.integers(0, _ID_POOL - 1)), draw(st.integers(min_value=0, max_value=b)))
        for _ in range(n_low)
    )

    return CondScenario(test_threshold=b, model_threshold=a, rows=tuple(rows))


def _cond_sql(s: CondScenario) -> str:
    return f"SELECT id, g FROM orders WHERE g > {s.model_threshold}"


def _cond_manifest(s: CondScenario) -> Manifest:
    nodes = [
        _source_node("orders"),
        _unique_test("orders", column="id", where=f"g > {s.test_threshold}"),
        _model_node(_cond_sql(s), depends_on=frozenset({"source.test.raw.orders"})),
    ]
    return _manifest(*nodes)


@given(s=_cond_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_conditionally_activated_keys_are_unique_over_materialized_rows(
    oracle_con: duckdb.DuckDBPyConnection, s: CondScenario
) -> None:
    """A conditional ``unique`` key promoted by activation is genuinely unique over the
    materialized rows. The source data honors the conditional declaration, so an
    over-eager activation (promoting the key when the model filter does not restrict to
    the predicate subset) surfaces as a duplicate the data check catches."""
    keys = _promoted_keys(_cond_manifest(s))
    tables: list[Table] = [("orders", ("id", "g"), s.rows)]
    _assert_keys_sound(oracle_con, tables, _cond_sql(s), keys)
