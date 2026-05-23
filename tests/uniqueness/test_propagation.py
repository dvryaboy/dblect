"""Tests for the per-scope uniqueness fact propagation pass.

Each transfer rule (projection, JOIN, GROUP BY, DISTINCT, UNION, WHERE) is
exercised in isolation, and a few composed cases match the motivating shape
of dbt models (CTEs that pass-through ref'd models then join).
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlglot import Expr

from dblect.sql import parse_sql
from dblect.uniqueness import (
    UniquenessFact,
    UniquenessSource,
    facts_from_tree,
    propagate_facts,
    top_scope_facts,
)


def _parse(sql: str) -> Expr:
    return parse_sql(sql, dialect="duckdb")


def _model_facts(
    model_uid: str, *keys: tuple[str, ...]
) -> Mapping[str, tuple[UniquenessFact, ...]]:
    return {
        model_uid: tuple(
            UniquenessFact(
                model_unique_id=model_uid,
                columns=frozenset(cols),
                source=UniquenessSource.DBT_UNIQUE_TEST,
                detail=None,
            )
            for cols in keys
        )
    }


def _top_keys(
    sql: str,
    *,
    model_facts: Mapping[str, tuple[UniquenessFact, ...]] | None = None,
    model_name_to_uid: Mapping[str, str] | None = None,
) -> frozenset[frozenset[str]]:
    tree = _parse(sql)
    prop = propagate_facts(
        tree,
        model_facts=model_facts or {},
        model_name_to_uid=model_name_to_uid or {},
    )
    sf = top_scope_facts(tree, prop)
    return sf.keys if sf is not None else frozenset()


# --- Top-level structural shapes (formerly the facts_from_sql cases) ---


def test_distinct_proves_uniqueness_on_full_tuple() -> None:
    assert _top_keys("select distinct a, b from t") == frozenset({frozenset({"a", "b"})})


def test_group_by_bare_columns_proves_uniqueness() -> None:
    assert _top_keys("select a, b, sum(x) from t group by a, b") == frozenset(
        {frozenset({"a", "b"})}
    )


def test_group_by_positional_is_not_proven() -> None:
    # `GROUP BY 1, 2` is positional; the pass conservatively can't model it.
    assert _top_keys("select a, b from t group by 1, 2") == frozenset()


def test_group_by_expression_is_not_proven() -> None:
    sql = "select date_trunc('day', ts) as d, sum(x) from t group by date_trunc('day', ts)"
    assert _top_keys(sql) == frozenset()


def test_select_without_distinct_or_group_proves_nothing_on_unknown_source() -> None:
    assert _top_keys("select a, b from t") == frozenset()


def test_union_distinct_proves_full_tuple_uniqueness() -> None:
    assert _top_keys("select a from t1 union select a from t2") == frozenset({frozenset({"a"})})


def test_union_all_proves_nothing() -> None:
    assert _top_keys("select a from t1 union all select a from t2") == frozenset()


# --- Propagation through CTEs and projections ---


def test_cte_passthrough_carries_model_keys() -> None:
    sql = "with x as (select * from realx) select * from x"
    keys = _top_keys(
        sql,
        model_facts=_model_facts("model.realx", ("id",)),
        model_name_to_uid={"realx": "model.realx"},
    )
    assert keys == frozenset({frozenset({"id"})})


def test_projection_rename_renames_keys() -> None:
    # input `id` projected as `pk` should yield a key on `pk`.
    sql = "select id as pk from realx"
    keys = _top_keys(
        sql,
        model_facts=_model_facts("model.realx", ("id",)),
        model_name_to_uid={"realx": "model.realx"},
    )
    assert keys == frozenset({frozenset({"pk"})})


def test_projection_dropping_key_column_drops_the_fact() -> None:
    # The fact is on `id`, but we only project `name`. The fact can't be
    # expressed in the output column space.
    sql = "select name from realx"
    keys = _top_keys(
        sql,
        model_facts=_model_facts("model.realx", ("id",)),
        model_name_to_uid={"realx": "model.realx"},
    )
    assert keys == frozenset()


def test_where_filter_preserves_keys() -> None:
    sql = "select id from realx where status = 'active'"
    keys = _top_keys(
        sql,
        model_facts=_model_facts("model.realx", ("id",)),
        model_name_to_uid={"realx": "model.realx"},
    )
    assert keys == frozenset({frozenset({"id"})})


# --- JOIN behavior ---


def test_join_on_unique_side_preserves_left_keys() -> None:
    # `dim` is unique on `id`; joining on dim.id ensures no fanout.
    sql = "select f.id, d.label from facts f join dim d on f.id = d.id"
    keys = _top_keys(
        sql,
        model_facts={
            **_model_facts("model.facts", ("id",)),
            **_model_facts("model.dim", ("id",)),
        },
        model_name_to_uid={"facts": "model.facts", "dim": "model.dim"},
    )
    assert keys == frozenset({frozenset({"id"})})


def test_join_on_non_unique_side_drops_keys() -> None:
    # `dim` is unique on `id`, but we join on `segment` — may fan out.
    sql = "select f.id from facts f join dim d on f.segment = d.segment"
    keys = _top_keys(
        sql,
        model_facts={
            **_model_facts("model.facts", ("id",)),
            **_model_facts("model.dim", ("id",)),
        },
        model_name_to_uid={"facts": "model.facts", "dim": "model.dim"},
    )
    assert keys == frozenset()


def test_cross_join_drops_keys() -> None:
    sql = "select f.id from facts f cross join dim d"
    keys = _top_keys(
        sql,
        model_facts={
            **_model_facts("model.facts", ("id",)),
            **_model_facts("model.dim", ("id",)),
        },
        model_name_to_uid={"facts": "model.facts", "dim": "model.dim"},
    )
    assert keys == frozenset()


def test_motivating_two_cte_join_shape() -> None:
    # The pattern the propagation work was designed for: two CTEs that
    # pass-through ref'd models, joined on their model keys.
    sql = (
        "with x as (select * from realx), "
        "     y as (select * from realy) "
        "select x.id, y.label from x join y on x.id = y.id"
    )
    keys = _top_keys(
        sql,
        model_facts={
            **_model_facts("model.realx", ("id",)),
            **_model_facts("model.realy", ("id",)),
        },
        model_name_to_uid={"realx": "model.realx", "realy": "model.realy"},
    )
    assert keys == frozenset({frozenset({"id"})})


# --- GROUP BY ---


def test_group_by_replaces_input_keys() -> None:
    # Even though `realx.id` is unique, the GROUP BY changes the row identity.
    sql = "select customer_id, count(*) as n from realx group by customer_id"
    keys = _top_keys(
        sql,
        model_facts=_model_facts("model.realx", ("id",)),
        model_name_to_uid={"realx": "model.realx"},
    )
    assert keys == frozenset({frozenset({"customer_id"})})


# --- Lineage tracking ---


def test_propagated_fact_carries_derivation_chain() -> None:
    tree = _parse("with x as (select * from realx) select * from x")
    out = facts_from_tree(
        "model.pkg.consumer",
        tree,
        model_facts=_model_facts("model.realx", ("id",)),
        model_name_to_uid={"realx": "model.realx"},
    )
    assert len(out) == 1
    fact = out[0]
    assert fact.columns == frozenset({"id"})
    assert fact.source is UniquenessSource.PROPAGATED
    assert len(fact.derived_from) == 1
    assert fact.derived_from[0].model_unique_id == "model.realx"


def test_structural_proof_facts_have_empty_chain() -> None:
    tree = _parse("select distinct a from t")
    out = facts_from_tree("model.pkg.x", tree, model_facts={}, model_name_to_uid={})
    assert len(out) == 1
    assert out[0].source is UniquenessSource.STRUCTURAL_PROOF
    assert out[0].derived_from == ()


# --- Scope keying ---


def test_propagation_map_is_keyed_by_node_identity() -> None:
    # The detector consumes the propagation map by `id(node)` lookup; if a
    # refactor changed the keying, both fact-grounded detectors would
    # silently lose coverage on every nested scope. Pin the contract.
    tree = _parse("with x as (select id from realx) select id from x")
    prop = propagate_facts(
        tree,
        model_facts=_model_facts("model.realx", ("id",)),
        model_name_to_uid={"realx": "model.realx"},
    )
    assert id(tree) in prop
    assert prop[id(tree)].keys == frozenset({frozenset({"id"})})
