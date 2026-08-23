"""The generated ``models`` stubs.

``dblect init`` reads the manifest and writes a typed ``models`` proxy so an editor
can autocomplete ``models.stg_orders.order_id`` and type-check it. The generator is
the Prisma/dlt pattern: a file in its own package, regenerated on manifest change,
never hand-edited. These pin what it emits: a class per data-flow node with a
``ColumnProxy`` attribute per documented column, and a ``models`` value typed as the
namespace of those classes. The output must be importable Python.
"""

from __future__ import annotations

import compileall

from dblect.contracts.stubs import generate_stub_module, model_class_name
from dblect.manifest import ResourceType
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node


def test_class_name_is_pascal_cased_and_prefixed() -> None:
    assert model_class_name("stg_orders") == "_StgOrders"
    assert model_class_name("fct_orders") == "_FctOrders"


def test_emits_a_class_per_node_with_column_attributes() -> None:
    manifest = _manifest(
        _node("model.shop.stg_orders", columns=_cols("order_id", "customer_id")),
        _node("model.shop.fct_orders", columns=_cols("order_id", "order_total")),
    )
    src = generate_stub_module(manifest)
    assert "class _StgOrders(ModelProxy):" in src
    assert "    order_id: ColumnProxy" in src
    assert "    customer_id: ColumnProxy" in src
    assert "class _FctOrders(ModelProxy):" in src
    assert "    order_total: ColumnProxy" in src


def test_models_namespace_binds_each_node_by_name() -> None:
    manifest = _manifest(_node("model.shop.stg_orders", columns=_cols("order_id")))
    src = generate_stub_module(manifest)
    assert "stg_orders: _StgOrders" in src
    assert "models = " in src  # a runtime value, typed as the namespace


def test_includes_sources_and_seeds() -> None:
    manifest = _manifest(
        _node("model.shop.orders", columns=_cols("id")),
        _node("source.shop.raw.payments", kind=ResourceType.SOURCE, columns=_cols("amount")),
        _node("seed.shop.raw_customers", kind=ResourceType.SEED, columns=_cols("id")),
    )
    src = generate_stub_module(manifest)
    assert "class _Payments(ModelProxy):" in src
    assert "class _RawCustomers(ModelProxy):" in src


def test_node_with_no_columns_still_gets_a_class() -> None:
    src = generate_stub_module(_manifest(_node("model.shop.bare")))
    assert "class _Bare(ModelProxy):" in src
    assert "pass" in src  # an empty body is still valid


def test_generated_module_is_importable_python(tmp_path: object) -> None:
    import pathlib

    assert isinstance(tmp_path, pathlib.Path)
    manifest = _manifest(
        _node("model.shop.stg_orders", columns=_cols("order_id")),
        _node("model.shop.fct_orders", columns=_cols("order_total")),
    )
    path = tmp_path / "models.py"
    path.write_text(generate_stub_module(manifest))
    assert compileall.compile_file(str(path), quiet=1)
