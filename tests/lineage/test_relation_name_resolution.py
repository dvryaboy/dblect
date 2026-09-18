"""``build_name_to_source``: the single owner of the compiled-SQL name resolution
convention (see its docstring in ``builder.py``).

Pins the property CodeRabbit flagged on PR #270: a bare relation name alone does not
disambiguate two relations that share it across different schemas. The qualified key
(:attr:`Node.qualified_relation_name`) must resolve each to its own ``SourceRef``,
never one clobbering or merging into the other.
"""

from __future__ import annotations

from dblect.lineage.builder import build_name_to_source
from dblect.lineage.graph import SourceKind, SourceRef
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source


def test_qualified_keys_resolve_two_same_named_sources_in_different_schemas() -> None:
    a = _source("source.shop.schema_a.orders", name="orders", schema="schema_a")
    b = _source("source.shop.schema_b.orders", name="orders", schema="schema_b")

    name_to_source = build_name_to_source(_manifest(a, b))

    assert name_to_source["schema_a.orders"] == SourceRef(SourceKind.SOURCE, a.unique_id)
    assert name_to_source["schema_b.orders"] == SourceRef(SourceKind.SOURCE, b.unique_id)


def test_a_model_in_a_different_schema_does_not_steal_a_sources_qualified_entry() -> None:
    # The exact shape CodeRabbit called out: a model must not replace a source with the
    # same bare name when the two live in different schemas, since they are genuinely
    # distinct relations. Qualified by schema, they key separately.
    src = _source("source.shop.raw.orders", name="orders", schema="raw")
    model = _node(
        "model.shop.other.orders", "select 1", raw="select 1", name="orders", schema="other"
    )

    name_to_source = build_name_to_source(_manifest(src, model))

    assert name_to_source["raw.orders"] == SourceRef(SourceKind.SOURCE, src.unique_id)
    assert name_to_source["other.orders"] == SourceRef(SourceKind.MODEL, model.unique_id)


def test_model_still_wins_on_a_true_same_schema_identity_collision() -> None:
    # A model and a source sharing both schema and name is a genuine identity collision
    # (dbt's own ref() convention prefers the model); that precedence is unchanged.
    src = _source("source.shop.analytics.orders", name="orders", schema="analytics")
    model = _node(
        "model.shop.analytics.orders", "select 1", raw="select 1", name="orders", schema="analytics"
    )

    name_to_source = build_name_to_source(_manifest(src, model))

    assert name_to_source["analytics.orders"] == SourceRef(SourceKind.MODEL, model.unique_id)
