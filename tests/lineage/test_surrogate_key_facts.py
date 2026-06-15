"""Surrogate-hash keys ground a candidate key on their input columns (#89).

A relation keyed by a surrogate hash (`to_hex(md5(concat(a, b)))`,
`dbt_utils.generate_surrogate_key([...])`) declares uniqueness on the hash
column, but the real key is the input tuple `(a, b)`: a downstream join on
`a, b` should see a key. The surrogate-key discoverer recognizes the hash idiom
in the model's own projection and grounds a key on the inputs, but only when the
shape is unambiguous (a recognized hash over structural combinations of columns
that are themselves output columns); anything opaque grounds nothing rather than
a wrong key.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.uniqueness import surrogate_key_discoverer
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType

_BQ = profile_for_adapter("bigquery")


def _model(uid: str, sql: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
    )


def _unique(uid: str, *, column: str, target: str) -> Node:
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
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}),
        attached_node=target,
    )


def _combination(uid: str, *, columns: list[str], target: str) -> Node:
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
            name="unique_combination_of_columns",
            kwargs={"combination_of_columns": columns},
        ),
        attached_node=target,
    )


def _manifest(*nodes: Node) -> Manifest:
    return Manifest(
        schema_version="v12", adapter_type="bigquery", nodes={n.unique_id: n for n in nodes}
    )


def _keys(*nodes: Node) -> set[frozenset[str]]:
    facts = list(surrogate_key_discoverer(_BQ).discover(_manifest(*nodes), name_to_source={}))
    return {k for f in facts for k in f.value.keys}


def test_unique_on_a_surrogate_hash_grounds_a_key_on_the_inputs() -> None:
    model = _model(
        "model.shop.m",
        "SELECT TO_HEX(MD5(CONCAT(a, '-', b))) AS sk, a, b FROM up",
    )
    test = _unique("test.shop.u", column="sk", target=model.unique_id)
    assert frozenset({"a", "b"}) in _keys(model, test)


def test_dbt_utils_surrogate_key_shape_is_recognized() -> None:
    # The compiled dbt_utils.generate_surrogate_key shape: coalesce+cast inside the
    # concat, hashed. The structural wrappers must not defeat recognition.
    model = _model(
        "model.shop.m",
        "SELECT MD5(CONCAT("
        "COALESCE(CAST(a AS STRING), '_null_'), '-', "
        "COALESCE(CAST(b AS STRING), '_null_'))) AS sk, a, b FROM up",
    )
    test = _unique("test.shop.u", column="sk", target=model.unique_id)
    assert frozenset({"a", "b"}) in _keys(model, test)


def test_inputs_not_projected_grounds_no_input_key() -> None:
    # The hash inputs are not output columns, so {a, b} is not a key the relation
    # can express; grounding it would be a wrong key.
    model = _model("model.shop.m", "SELECT MD5(CONCAT(a, b)) AS sk FROM up")
    test = _unique("test.shop.u", column="sk", target=model.unique_id)
    assert _keys(model, test) == set()


def test_hash_over_an_opaque_expression_grounds_nothing() -> None:
    # An unknown function wraps a column inside the hash, so the input tuple is not
    # the plain columns; stay silent rather than ground a wrong key.
    model = _model(
        "model.shop.m",
        "SELECT MD5(CONCAT(a, SOME_UDF(b))) AS sk, a, b FROM up",
    )
    test = _unique("test.shop.u", column="sk", target=model.unique_id)
    assert _keys(model, test) == set()


def test_plain_column_unique_test_grounds_nothing() -> None:
    model = _model("model.shop.m", "SELECT id, amount FROM up")
    test = _unique("test.shop.u", column="id", target=model.unique_id)
    assert _keys(model, test) == set()


def test_composite_key_substitutes_the_hash_member() -> None:
    model = _model(
        "model.shop.m",
        "SELECT x, MD5(CONCAT(a, b)) AS sk, a, b FROM up",
    )
    test = _combination("test.shop.uc", columns=["x", "sk"], target=model.unique_id)
    assert frozenset({"x", "a", "b"}) in _keys(model, test)


def test_input_key_flows_end_to_end_through_the_property() -> None:
    # Wired into uniqueness_property: the inputs key is grounded and propagates, so
    # a downstream model that re-keys on the inputs is seen as unique on them.
    from dblect.lineage.builder import build_relation_graph
    from dblect.lineage.properties.uniqueness import uniqueness_property
    from dblect.lineage.property import propagate

    model = _model("model.shop.m", "SELECT TO_HEX(MD5(CONCAT(a, b))) AS sk, a, b FROM up")
    test = _unique("test.shop.u", column="sk", target=model.unique_id)
    manifest = _manifest(model, test)
    graph = build_relation_graph(manifest, dialect="bigquery").graph
    anns = propagate(graph, uniqueness_property(manifest, _BQ))
    keys = anns[SourceRef(SourceKind.MODEL, "model.shop.m")].value.keys
    assert frozenset({"a", "b"}) in keys  # the inputs key
    assert frozenset({"sk"}) in keys  # the declared hash key still holds too
