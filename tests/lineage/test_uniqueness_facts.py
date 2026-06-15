"""Uniqueness discoverers: declarations become relation-scoped candidate-key facts.

Each discoverer reads one declaration channel off a dbt manifest and emits a
``Fact[CandidateKeySet, SourceRef]`` whose value names a single candidate key;
``grounding`` later unions several facts at one relation through the lattice
meet. The tests build dblect-shaped manifests directly so each discoverer's
contract is pinned against the typed ``Manifest``. They are total within their
axis (a non-uniqueness declaration grounds nothing), sound by omission (a
disabled or ``where``-conditional test grounds nothing), and never emit a
top-valued (empty) key set.
"""

from __future__ import annotations

from collections.abc import Mapping

from hypothesis import given
from hypothesis import strategies as st

from dblect.adapters import profile_for_adapter
from dblect.lineage.facts.model import Declared, DeclaredSource, NativeConstraint, Predicate
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.uniqueness import (
    CandidateKeySet,
    native_key_discoverer,
    unique_combination_discoverer,
    unique_test_discoverer,
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


def _test(
    uid: str,
    *,
    name: str,
    kwargs: Mapping[str, object],
    target: str,
    where: str | None = None,
    enabled: bool = True,
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
        test_metadata=DbtTestMetadata(name=name, kwargs=dict(kwargs), enabled=enabled, where=where),
        attached_node=target,
    )


def _unique(
    uid: str, *, column: str, target: str, where: str | None = None, enabled: bool = True
) -> Node:
    return _test(
        uid,
        name="unique",
        kwargs={"column_name": column},
        target=target,
        where=where,
        enabled=enabled,
    )


def _key(*cols: str) -> frozenset[str]:
    return frozenset(cols)


# --- unique test discoverer --------------------------------------------------


def test_unique_test_on_model_grounds_single_column_key() -> None:
    model = _model("model.shop.dim_customer")
    test = _unique("test.shop.u", column="customer_id", target=model.unique_id)
    facts = list(unique_test_discoverer().discover(_manifest(model, test), name_to_source={}))
    assert len(facts) == 1
    fact = facts[0]
    assert fact.scope == SourceRef(SourceKind.MODEL, model.unique_id)
    assert fact.value == CandidateKeySet.of(_key("customer_id"))
    assert fact.provenance == Declared(DeclaredSource.DBT_GENERIC_TEST)


def test_unique_test_on_source_grounds_on_the_source_relation() -> None:
    src = _source("source.shop.raw.orders")
    test = _unique("test.shop.u", column="id", target=src.unique_id)
    facts = list(unique_test_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert facts[0].scope == SourceRef(SourceKind.SOURCE, src.unique_id)


def test_unique_test_column_is_case_folded() -> None:
    model = _model("model.shop.dim_customer")
    test = _unique("test.shop.u", column="CustomerID", target=model.unique_id)
    facts = list(unique_test_discoverer().discover(_manifest(model, test), name_to_source={}))
    assert facts[0].value == CandidateKeySet.of(_key("customerid"))


def test_conditional_unique_test_is_captured_with_its_predicate() -> None:
    """A ``where`` filter does not drop the fact: it is captured carrying the
    predicate, so an activation step can consume it. Grounding still does not fold
    a conditional key into the unconditional key set (pinned in the grounding
    tests)."""
    model = _model("model.shop.dim_customer")
    test = _unique("test.shop.u", column="customer_id", target=model.unique_id, where="active")
    facts = list(unique_test_discoverer().discover(_manifest(model, test), name_to_source={}))
    assert len(facts) == 1
    assert facts[0].value == CandidateKeySet.of(_key("customer_id"))
    assert facts[0].condition == Predicate("active")


def test_disabled_unique_test_grounds_nothing() -> None:
    model = _model("model.shop.dim_customer")
    test = _unique("test.shop.u", column="customer_id", target=model.unique_id, enabled=False)
    facts = list(unique_test_discoverer().discover(_manifest(model, test), name_to_source={}))
    assert facts == []


def test_non_uniqueness_test_is_ignored() -> None:
    model = _model("model.shop.dim_customer")
    nn = _test(
        "test.shop.nn",
        name="not_null",
        kwargs={"column_name": "customer_id"},
        target=model.unique_id,
    )
    facts = list(unique_test_discoverer().discover(_manifest(model, nn), name_to_source={}))
    assert facts == []


# --- unique_combination_of_columns discoverer --------------------------------


def test_unique_combination_grounds_a_composite_key() -> None:
    model = _model("model.shop.fct_orders")
    test = _test(
        "test.shop.uc",
        name="dbt_utils.unique_combination_of_columns",
        kwargs={"combination_of_columns": ["order_id", "line_id"]},
        target=model.unique_id,
    )
    facts = list(
        unique_combination_discoverer().discover(_manifest(model, test), name_to_source={})
    )
    assert len(facts) == 1
    assert facts[0].value == CandidateKeySet.of(_key("order_id", "line_id"))
    assert facts[0].provenance == Declared(DeclaredSource.DBT_UTILS_TEST)


def test_unique_combination_without_a_column_list_grounds_nothing() -> None:
    model = _model("model.shop.fct_orders")
    test = _test(
        "test.shop.uc",
        name="dbt_utils.unique_combination_of_columns",
        kwargs={"combination_of_columns": "not-a-list"},
        target=model.unique_id,
    )
    facts = list(
        unique_combination_discoverer().discover(_manifest(model, test), name_to_source={})
    )
    assert facts == []


# --- native key constraint discoverer ----------------------------------------


def test_model_level_primary_key_grounds_a_composite_key() -> None:
    model = _model(
        "model.shop.fct_orders",
        constraints=(
            ConstraintSpec(type=ConstraintType.PRIMARY_KEY, columns=("order_id", "line_id")),
        ),
    )
    facts = list(
        native_key_discoverer(profile_for_adapter("duckdb")).discover(
            _manifest(model), name_to_source={}
        )
    )
    assert len(facts) == 1
    assert facts[0].value == CandidateKeySet.of(_key("order_id", "line_id"))
    assert isinstance(facts[0].provenance, NativeConstraint)


def test_column_level_unique_constraint_grounds_a_single_column_key() -> None:
    model = _model(
        "model.shop.dim_customer",
        columns={
            "email": Column(
                name="email",
                data_type="varchar",
                description=None,
                constraints=(ConstraintSpec(type=ConstraintType.UNIQUE),),
            )
        },
    )
    facts = list(
        native_key_discoverer(profile_for_adapter("duckdb")).discover(
            _manifest(model), name_to_source={}
        )
    )
    assert facts[0].value == CandidateKeySet.of(_key("email"))


def test_native_non_key_constraint_is_ignored() -> None:
    model = _model(
        "model.shop.dim_customer",
        constraints=(ConstraintSpec(type=ConstraintType.NOT_NULL, columns=("email",)),),
    )
    facts = list(
        native_key_discoverer(profile_for_adapter("duckdb")).discover(
            _manifest(model), name_to_source={}
        )
    )
    assert facts == []


def test_native_key_enforcement_is_adapter_aware() -> None:
    """The enforcement flag is descriptive provenance: duckdb enforces a UNIQUE
    constraint on write, Snowflake treats it as advisory."""
    spec = (ConstraintSpec(type=ConstraintType.PRIMARY_KEY, columns=("id",)),)
    duck = _model("model.shop.m", constraints=spec)
    snow = _model("model.shop.m", constraints=spec)
    duck_fact = next(
        iter(
            native_key_discoverer(profile_for_adapter("duckdb")).discover(
                _manifest(duck), name_to_source={}
            )
        )
    )
    snow_fact = next(
        iter(
            native_key_discoverer(profile_for_adapter("snowflake")).discover(
                _manifest(snow), name_to_source={}
            )
        )
    )
    assert isinstance(duck_fact.provenance, NativeConstraint)
    assert isinstance(snow_fact.provenance, NativeConstraint)
    assert duck_fact.provenance.enforced_on_write
    assert not snow_fact.provenance.enforced_on_write


# --- per-discoverer PBT: facts are a function of the documented input --------

_cols = st.sampled_from(["id", "email", "order_id", "line_id", "customer_id"])


@given(st.lists(_cols, min_size=1, max_size=3, unique=True))
def test_unique_combination_fact_mirrors_its_columns(columns: list[str]) -> None:
    """The discoverer never invents or drops a column, and never emits an empty
    key set: the grounded key is exactly the declared combination, case-folded."""
    model = _model("model.shop.m")
    test = _test(
        "test.shop.uc",
        name="dbt_utils.unique_combination_of_columns",
        kwargs={"combination_of_columns": list(columns)},
        target=model.unique_id,
    )
    facts = list(
        unique_combination_discoverer().discover(_manifest(model, test), name_to_source={})
    )
    assert len(facts) == 1
    value = facts[0].value
    assert value.keys == frozenset({frozenset(c.lower() for c in columns)})
    assert value != CandidateKeySet.of()  # never a top-valued claim
