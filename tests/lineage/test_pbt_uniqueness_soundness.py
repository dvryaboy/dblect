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
  distinct forms.
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
from dataclasses import dataclass

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
from tests.lineage._duckdb_oracle import Table, materialized, scalar

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


def _assert_keys_sound(tables: Sequence[Table], model_sql: str, keys: frozenset[Key]) -> None:
    """Materialize ``tables`` and the model in duckdb; assert every key in ``keys`` has
    as many distinct key tuples as the model has rows (so it is genuinely unique)."""
    with materialized(tables, model_sql) as con:
        total = scalar(con, "SELECT COUNT(*) FROM _m")
        for key in keys:
            cols = ", ".join(sorted(key))
            distinct = scalar(con, f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM _m)")
            assert distinct == total, (
                f"unsound key {sorted(key)}: {total} rows but {distinct} distinct tuples "
                f"for sql={model_sql!r} tables={tables!r}"
            )


def _source_node(name: str, schema: str = "raw") -> Node:
    return Node(
        unique_id=f"source.test.{schema}.{name}",
        name=name,
        resource_type=ResourceType.SOURCE,
        fqn=("test", name),
        package_name="test",
        schema=schema,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _unique_test(source_name: str, *, column: str, where: str | None = None) -> Node:
    target = f"source.test.raw.{source_name}"
    suffix = "_cond" if where is not None else ""
    return Node(
        unique_id=f"test.test.{source_name}_{column}_unique{suffix}",
        name=f"{source_name}_{column}_unique{suffix}",
        resource_type=ResourceType.OTHER,
        fqn=("test", f"{source_name}_{column}_unique"),
        package_name="test",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}, where=where),
        attached_node=target,
    )


def _model_node(sql: str, *, depends_on: frozenset[str]) -> Node:
    return Node(
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
        depends_on=depends_on,
    )


# --- unconditional shapes ---------------------------------------------------------


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
    shape: str  # filter | inner_join | left_join | group_by | distinct
    select_cols: tuple[str, ...]
    left_join_col: str | None = None
    right_join_col: str | None = None
    filter_col: str | None = None
    filter_threshold: int | None = None
    group_cols: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Scenario:
    sources: tuple[SourceSpec, ...]
    model: ModelSpec
    data: tuple[tuple[str, tuple[tuple[int, ...], ...]], ...]


_S0 = SourceSpec(name="s0", key_col="k0", plain_cols=("a0", "b0"))
_S1 = SourceSpec(name="s1", key_col="k1", plain_cols=("a1", "b1"))
_KEY_DOMAIN = 64
_PLAIN_DOMAIN = 4


@st.composite
def _rows(draw: st.DrawFn, source: SourceSpec) -> tuple[tuple[int, ...], ...]:
    """Rows for a source: the key column is distinct non-null (so ``unique`` is a true
    key), every other column a small-domain int (to force join matches and duplicates)."""
    n = draw(st.integers(min_value=0, max_value=8))
    keys = draw(
        st.lists(
            st.integers(min_value=0, max_value=_KEY_DOMAIN - 1), min_size=n, max_size=n, unique=True
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
        model = ModelSpec(
            shape=shape,
            select_cols=("k0", "a1"),
            left_join_col=draw(st.sampled_from(_S0.columns)),
            right_join_col=draw(st.sampled_from(_S1.columns)),
        )
    elif shape == "filter":
        model = ModelSpec(
            shape=shape,
            select_cols=("k0", "a0"),
            filter_col=draw(st.sampled_from(_S0.columns)),
            filter_threshold=draw(st.integers(min_value=0, max_value=_PLAIN_DOMAIN)),
        )
    else:  # group_by | distinct
        cols = tuple(
            sorted(
                draw(st.lists(st.sampled_from(_S0.columns), min_size=1, max_size=3, unique=True))
            )
        )
        select = (*cols, "n") if shape == "group_by" else cols
        model = ModelSpec(shape=shape, select_cols=select, group_cols=cols)

    data = tuple((s.name, draw(_rows(s))) for s in sources)
    return Scenario(sources=sources, model=model, data=data)


def _scenario_sql(m: ModelSpec) -> str:
    if m.shape in ("inner_join", "left_join"):
        join = "INNER JOIN" if m.shape == "inner_join" else "LEFT JOIN"
        return (
            f"SELECT s0.k0 AS k0, s1.a1 AS a1 "
            f"FROM s0 {join} s1 ON s0.{m.left_join_col} = s1.{m.right_join_col}"
        )
    if m.shape == "filter":
        return f"SELECT k0, a0 FROM s0 WHERE {m.filter_col} >= {m.filter_threshold}"
    assert m.group_cols is not None
    cols = ", ".join(m.group_cols)
    if m.shape == "group_by":
        return f"SELECT {cols}, COUNT(*) AS n FROM s0 GROUP BY {cols}"
    return f"SELECT DISTINCT {cols} FROM s0"


def _scenario_manifest(s: Scenario) -> Manifest:
    nodes: list[Node] = []
    for src in s.sources:
        nodes.append(_source_node(src.name))
        nodes.append(_unique_test(src.name, column=src.key_col))
    nodes.append(
        _model_node(
            _scenario_sql(s.model),
            depends_on=frozenset(f"source.test.raw.{src.name}" for src in s.sources),
        )
    )
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


@given(_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_unconditional_promoted_keys_are_unique_over_materialized_rows(s: Scenario) -> None:
    """Every key the analyzer promotes for a filter/join/group/distinct model is
    genuinely unique over the duckdb-materialized rows. The test never recomputes
    which keys should survive; the data is the judge."""
    keys = _promoted_keys(_scenario_manifest(s))
    output_cols = {c.lower() for c in s.model.select_cols}
    for key in keys:
        assert key <= output_cols, f"key {sorted(key)} not in outputs {sorted(output_cols)}"
    data_by_name = dict(s.data)
    tables: list[Table] = [(src.name, src.columns, data_by_name[src.name]) for src in s.sources]
    _assert_keys_sound(tables, _scenario_sql(s.model), keys)


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
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


@given(_cond_scenario())
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_conditionally_activated_keys_are_unique_over_materialized_rows(s: CondScenario) -> None:
    """A conditional ``unique`` key promoted by activation is genuinely unique over the
    materialized rows. The source data honors the conditional declaration, so an
    over-eager activation (promoting the key when the model filter does not restrict to
    the predicate subset) surfaces as a duplicate the data check catches."""
    keys = _promoted_keys(_cond_manifest(s))
    tables: list[Table] = [("orders", ("id", "g"), s.rows)]
    _assert_keys_sound(tables, _cond_sql(s), keys)
