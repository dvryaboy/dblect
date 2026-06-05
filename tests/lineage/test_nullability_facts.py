"""Nullability discoverers and the manifest-backed nullability_property.

The discoverers read declarations off a dbt manifest: a ``not_null`` generic
test and a native ``NOT NULL`` constraint each ground a column to NON_NULL. The
tests build dblect-shaped manifests directly (no JSON round-trip) so each
discoverer's contract is pinned against the typed `Manifest`, and one end-to-end
case threads a discovered fact through the propagator.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Declared, DeclaredSource, NativeConstraint
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.nullability import (
    Nullability,
    native_not_null_discoverer,
    not_null_test_discoverer,
    nullability_property,
)
from dblect.manifest import (
    Column,
    ConstraintSpec,
    ConstraintType,
    DbtTestMetadata,
    Manifest,
    Node,
    ResourceType,
)


def _manifest(*nodes: Node, adapter_type: str = "duckdb") -> Manifest:
    return Manifest(
        schema_version="v12",
        adapter_type=adapter_type,
        nodes={n.unique_id: n for n in nodes},
    )


def _model(
    uid: str, *, columns: Mapping[str, Column] = {}, constraints: tuple[ConstraintSpec, ...] = ()
) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code="select 1",
        original_file_path=None,
        columns=columns,
        constraints=constraints,
    )


def _source(uid: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.SOURCE,
        fqn=(uid,),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _not_null_test(
    uid: str, *, column: str, target: str, where: str | None = None, enabled: bool = True
) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.OTHER,
        fqn=(uid,),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(
            name="not_null",
            kwargs={"column_name": column},
            enabled=enabled,
            where=where,
        ),
        attached_node=target,
    )


# --- not_null test discoverer ------------------------------------------------


def test_not_null_test_on_source_column_grounds_non_null() -> None:
    src = _source("source.shop.raw.orders")
    test = _not_null_test("test.shop.nn", column="id", target=src.unique_id)
    facts = list(not_null_test_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert len(facts) == 1
    fact = facts[0]
    assert fact.scope == ColumnRef(SourceRef(SourceKind.SOURCE, src.unique_id), "id")
    assert fact.value is Nullability.NON_NULL
    assert fact.provenance == Declared(DeclaredSource.DBT_GENERIC_TEST)


def test_not_null_test_on_model_column_grounds_non_null() -> None:
    model = _model("model.shop.fct_orders")
    test = _not_null_test("test.shop.nn", column="order_id", target=model.unique_id)
    facts = list(not_null_test_discoverer().discover(_manifest(model, test), name_to_source={}))
    assert facts[0].scope == ColumnRef(SourceRef(SourceKind.MODEL, model.unique_id), "order_id")


def test_not_null_test_column_name_is_case_folded() -> None:
    src = _source("source.shop.raw.orders")
    test = _not_null_test("test.shop.nn", column="OrderId", target=src.unique_id)
    facts = list(not_null_test_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert facts[0].scope.column == "orderid"


def test_conditional_not_null_test_is_not_activated() -> None:
    """A ``where`` filter makes the assertion conditional; capturing it as an
    unconditional NON_NULL would over-claim, so it grounds nothing for now."""
    src = _source("source.shop.raw.orders")
    test = _not_null_test("test.shop.nn", column="id", target=src.unique_id, where="amount > 0")
    facts = list(not_null_test_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert facts == []


def test_disabled_not_null_test_grounds_nothing() -> None:
    src = _source("source.shop.raw.orders")
    test = _not_null_test("test.shop.nn", column="id", target=src.unique_id, enabled=False)
    facts = list(not_null_test_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert facts == []


def test_non_nullability_test_is_ignored() -> None:
    """A discoverer is total within its axis: a ``unique`` test is not its concern."""
    src = _source("source.shop.raw.orders")
    other = _not_null_test("test.shop.u", column="id", target=src.unique_id)
    other = Node(  # rebuild with a unique test rather than not_null
        unique_id=other.unique_id,
        name=other.name,
        resource_type=ResourceType.OTHER,
        fqn=other.fqn,
        package_name=other.package_name,
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=other.depends_on,
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": "id"}),
        attached_node=src.unique_id,
    )
    facts = list(not_null_test_discoverer().discover(_manifest(src, other), name_to_source={}))
    assert facts == []


# --- native NOT NULL constraint discoverer -----------------------------------


def test_column_level_not_null_constraint_grounds_non_null() -> None:
    model = _model(
        "model.shop.fct_orders",
        columns={
            "order_id": Column(
                name="order_id",
                data_type="bigint",
                description=None,
                constraints=(ConstraintSpec(type=ConstraintType.NOT_NULL),),
            )
        },
    )
    facts = list(native_not_null_discoverer("duckdb").discover(_manifest(model), name_to_source={}))
    assert len(facts) == 1
    fact = facts[0]
    assert fact.scope == ColumnRef(SourceRef(SourceKind.MODEL, model.unique_id), "order_id")
    assert fact.value is Nullability.NON_NULL
    assert isinstance(fact.provenance, NativeConstraint)


def test_model_level_not_null_constraint_grounds_each_named_column() -> None:
    model = _model(
        "model.shop.fct_orders",
        constraints=(
            ConstraintSpec(type=ConstraintType.NOT_NULL, columns=("order_id", "customer_id")),
        ),
    )
    facts = list(native_not_null_discoverer("duckdb").discover(_manifest(model), name_to_source={}))
    cols = {f.scope.column for f in facts}
    assert cols == {"order_id", "customer_id"}


def test_native_constraint_other_than_not_null_is_ignored() -> None:
    model = _model(
        "model.shop.fct_orders",
        columns={
            "order_id": Column(
                name="order_id",
                data_type="bigint",
                description=None,
                constraints=(ConstraintSpec(type=ConstraintType.PRIMARY_KEY),),
            )
        },
    )
    facts = list(native_not_null_discoverer("duckdb").discover(_manifest(model), name_to_source={}))
    assert facts == []


# --- nullability_property end-to-end -----------------------------------------


def test_nullability_property_flows_discovered_non_null_through_a_model() -> None:
    src = _source("source.shop.raw.orders")
    test = _not_null_test("test.shop.nn", column="id", target=src.unique_id)
    manifest = _manifest(src, test)
    src_ref = SourceRef(SourceKind.SOURCE, src.unique_id)
    graph = build_model_graph(
        model_uid="model.shop.fct",
        sql="SELECT u.id FROM orders u",
        name_to_source={"orders": src_ref},
        schema={"orders": {"id": "INT"}},
    )
    prop = nullability_property(manifest, name_to_source={"orders": src_ref})
    anns = propagate(graph, prop)
    leaf = ColumnRef(src_ref, "id")
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.shop.fct"), "id")
    assert anns[leaf].value is Nullability.NON_NULL
    assert anns[out].value is Nullability.NON_NULL


def test_nullability_property_leaves_undeclared_columns_unknown() -> None:
    src = _source("source.shop.raw.orders")
    manifest = _manifest(src)  # no not_null test
    src_ref = SourceRef(SourceKind.SOURCE, src.unique_id)
    graph = build_model_graph(
        model_uid="model.shop.fct",
        sql="SELECT u.id FROM orders u",
        name_to_source={"orders": src_ref},
        schema={"orders": {"id": "INT"}},
    )
    prop = nullability_property(manifest, name_to_source={"orders": src_ref})
    anns = propagate(graph, prop)
    out = ColumnRef(SourceRef(SourceKind.MODEL, "model.shop.fct"), "id")
    assert anns[out].value is Nullability.UNKNOWN


def test_nullability_property_accepts_extra_discoverers() -> None:
    """A caller can contribute its own discoverer; its facts ground alongside
    the built-ins."""
    src = _source("source.shop.raw.orders")
    src_ref = SourceRef(SourceKind.SOURCE, src.unique_id)

    class _AlwaysNonNull:
        def discover(self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]):  # type: ignore[no-untyped-def]
            from dblect.lineage.facts.model import Fact

            return (
                Fact(
                    scope=ColumnRef(src_ref, "id"),
                    value=Nullability.NON_NULL,
                    provenance=Declared(DeclaredSource.USER_ASSERTED),
                ),
            )

    graph = build_model_graph(
        model_uid="model.shop.fct",
        sql="SELECT u.id FROM orders u",
        name_to_source={"orders": src_ref},
        schema={"orders": {"id": "INT"}},
    )
    prop = nullability_property(
        _manifest(src), name_to_source={"orders": src_ref}, extra=(_AlwaysNonNull(),)
    )
    anns = propagate(graph, prop)
    assert anns[ColumnRef(src_ref, "id")].value is Nullability.NON_NULL
