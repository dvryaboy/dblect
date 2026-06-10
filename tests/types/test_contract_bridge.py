# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportAssignmentType=false
"""The fact bridge: registered contracts resolved against a manifest become
substrate facts, and every name that fails to resolve becomes a finding.

Resolution runs after the whole registry is populated, so a misspelled
``dbt_model`` is a reported issue and the remaining contracts still ground
their facts. Domain-type tags land on the magnitude column with companion
bindings into the same relation; ``PrimaryKey`` markers become candidate-key
facts that merge with dbt-test-sourced ones in ``collect``.
"""

from collections.abc import Mapping

from dblect.lineage.facts.grounding import collect
from dblect.lineage.facts.model import Declared, DeclaredSource
from dblect.lineage.facts.property import FactDiscoverer
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    Concrete,
    Dimension,
    PerRow,
    tagged,
)
from dblect.lineage.properties.uniqueness import CandidateKeySet, unique_test_discoverer
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import DbtTestMetadata
from dblect.types import (
    Currency,
    Decimal,
    Field,
    ForeignKey,
    ForeignKeyEdge,
    IssueCode,
    ModelContract,
    Money,
    PrimaryKey,
    contract_key_discoverer,
    contract_tag_discoverer,
    resolve_contracts,
)


class Revenue(Money):
    contains_tax: bool
    contains_discount: bool


def _node(
    uid: str,
    *,
    kind: ResourceType = ResourceType.MODEL,
    fqn: tuple[str, ...] | None = None,
    package: str = "shop",
) -> Node:
    name = uid.split(".")[-1]
    return Node(
        unique_id=uid,
        name=name,
        resource_type=kind,
        fqn=fqn if fqn is not None else (package, name),
        package_name=package,
        schema="analytics",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _unique_test(uid: str, *, column: str, target: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.OTHER,
        fqn=("shop", uid.split(".")[-1]),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}),
        attached_node=target,
    )


def _manifest(*nodes: Node) -> Manifest:
    return Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )


_CHARGES = _manifest(_node("model.shop.stg_charges"))
_CHARGES_SRC = SourceRef(SourceKind.MODEL, "model.shop.stg_charges")


# --- domain-tag facts ------------------------------------------------------------


def test_per_row_companion_binding_lands_on_the_magnitude_column() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        charge_amount: Money.columns(amount="charge_amount", currency="currency")

    resolved = resolve_contracts(_CHARGES)
    assert resolved.issues == ()
    (fact,) = resolved.tag_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "charge_amount")
    assert fact.value == tagged(dimension=Dimension.of(PerRow(ColumnRef(_CHARGES_SRC, "currency"))))
    assert fact.provenance == Declared(DeclaredSource.USER_ASSERTED)
    assert fact.detail is not None
    assert "StgCharges.charge_amount" in fact.detail


def test_pinned_currency_grounds_a_concrete_unit() -> None:
    class StgSales(ModelContract):
        dbt_model = "stg_charges"
        sale: Money(amount="sale_amount", currency=Currency.USD)

    resolved = resolve_contracts(_CHARGES)
    (fact,) = resolved.tag_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "sale_amount")
    assert fact.value == tagged(dimension=Dimension.of(Concrete("usd")))


def test_open_fields_bind_their_like_named_columns() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        amount: Money

    resolved = resolve_contracts(_CHARGES)
    (fact,) = resolved.tag_facts
    assert fact.scope == ColumnRef(_CHARGES_SRC, "amount")
    assert fact.value == tagged(dimension=Dimension.of(PerRow(ColumnRef(_CHARGES_SRC, "currency"))))


def test_nominal_facets_ride_as_concrete_or_per_row_bindings() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        net: Revenue.columns(amount="net_amount", contains_tax="taxed").refine(
            currency=Currency.USD, contains_discount=True
        )

    resolved = resolve_contracts(_CHARGES)
    (fact,) = resolved.tag_facts
    assert fact.value == tagged(
        dimension=Dimension.of(Concrete("usd")),
        nominal={
            "contains_tax": PerRow(ColumnRef(_CHARGES_SRC, "taxed")),
            "contains_discount": Concrete("true"),
        },
    )


def test_a_type_with_no_magnitude_grounds_nothing() -> None:
    from dblect.types import Country, DomainType

    class Locale(DomainType):
        country: Country

    class M(ModelContract):
        dbt_model = "stg_charges"
        locale: Locale

    resolved = resolve_contracts(_CHARGES)
    assert resolved.tag_facts == ()
    assert resolved.issues == ()


def test_a_type_with_two_magnitudes_is_malformed() -> None:
    from dblect.types import DomainType

    class Pair(DomainType):
        first: Decimal
        second: Decimal

    class M(ModelContract):
        dbt_model = "stg_charges"
        pair: Pair

    resolved = resolve_contracts(_CHARGES)
    assert resolved.tag_facts == ()
    (issue,) = resolved.issues
    assert issue.code is IssueCode.MALFORMED_DECLARATION
    assert issue.field == "pair"


def test_out_of_domain_enum_literal_is_a_finding_not_a_tag() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        amount: Money.refine(currency="ZZZ")

    resolved = resolve_contracts(_CHARGES)
    assert resolved.tag_facts == ()
    (issue,) = resolved.issues
    assert issue.code is IssueCode.OUT_OF_DOMAIN_VALUE
    assert issue.field == "amount"
    assert "ZZZ" in issue.message


# --- dbt_model resolution ----------------------------------------------------------


def test_misspelled_dbt_model_is_a_finding_and_spares_the_rest() -> None:
    class Typo(ModelContract):
        dbt_model = "stg_chargs"
        amount: Money(currency=Currency.USD)

    class Fine(ModelContract):
        dbt_model = "stg_charges"
        amount: Money(currency=Currency.USD)

    resolved = resolve_contracts(_CHARGES)
    (issue,) = resolved.issues
    assert issue.code is IssueCode.UNRESOLVED_MODEL
    assert issue.dbt_model == "stg_chargs"
    assert issue.contract.endswith("Typo")
    (fact,) = resolved.tag_facts  # Fine still grounds
    assert fact.scope == ColumnRef(_CHARGES_SRC, "amount")


def test_qualified_dbt_model_resolves_by_fqn_suffix() -> None:
    manifest = _manifest(
        _node("model.shop.fct_orders", fqn=("shop", "marts", "fct_orders")),
    )

    class FctOrders(ModelContract):
        dbt_model = "marts.fct_orders"
        total: Money(amount="total", currency=Currency.USD)

    resolved = resolve_contracts(manifest)
    assert resolved.issues == ()
    (fact,) = resolved.tag_facts
    assert fact.scope.source == SourceRef(SourceKind.MODEL, "model.shop.fct_orders")


def test_wrong_qualifier_does_not_resolve() -> None:
    manifest = _manifest(
        _node("model.shop.fct_orders", fqn=("shop", "marts", "fct_orders")),
    )

    class FctOrders(ModelContract):
        dbt_model = "staging.fct_orders"
        total: Money(amount="total", currency=Currency.USD)

    resolved = resolve_contracts(manifest)
    (issue,) = resolved.issues
    assert issue.code is IssueCode.UNRESOLVED_MODEL


def test_ambiguous_bare_name_is_a_finding() -> None:
    manifest = _manifest(
        _node("model.shop.dim_users", fqn=("shop", "dim_users")),
        _node("model.crm.dim_users", fqn=("crm", "dim_users"), package="crm"),
    )

    class DimUsers(ModelContract):
        dbt_model = "dim_users"
        amount: Money(currency=Currency.USD)

    resolved = resolve_contracts(manifest)
    (issue,) = resolved.issues
    assert issue.code is IssueCode.AMBIGUOUS_MODEL
    assert resolved.tag_facts == ()


# --- column validation against known schemas ----------------------------------------


def test_mapped_column_missing_from_known_schema_is_a_finding() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        charge_amount: Money.columns(amount="charge_amount", currency="currency")

    resolved = resolve_contracts(
        _CHARGES, known_columns={_CHARGES_SRC: frozenset({"charge_amount"})}
    )
    (issue,) = resolved.issues
    assert issue.code is IssueCode.UNKNOWN_COLUMN
    assert "currency" in issue.message
    assert resolved.tag_facts == ()  # the only claim rested on the missing companion


def test_open_field_with_no_backing_column_is_unsourced() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        charge_amount: Money.columns(amount="charge_amount")

    resolved = resolve_contracts(
        _CHARGES, known_columns={_CHARGES_SRC: frozenset({"charge_amount"})}
    )
    (issue,) = resolved.issues
    assert issue.code is IssueCode.UNSOURCED_FIELD
    assert issue.field == "charge_amount"
    assert "currency" in issue.message


def test_known_schema_with_all_columns_is_quiet() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        charge_amount: Money.columns(amount="charge_amount", currency="currency")

    resolved = resolve_contracts(
        _CHARGES, known_columns={_CHARGES_SRC: frozenset({"charge_amount", "currency"})}
    )
    assert resolved.issues == ()
    assert len(resolved.tag_facts) == 1


# --- key and foreign-key facts -------------------------------------------------------


def test_primary_key_marker_grounds_a_candidate_key() -> None:
    class FctOrders(ModelContract):
        dbt_model = "stg_charges"
        order_id: PrimaryKey

    resolved = resolve_contracts(_CHARGES)
    (fact,) = resolved.key_facts
    assert fact.scope == _CHARGES_SRC
    assert fact.value == CandidateKeySet.of(frozenset({"order_id"}))
    assert fact.provenance == Declared(DeclaredSource.USER_ASSERTED)


def test_two_primary_key_columns_form_one_composite_key() -> None:
    class Bridge(ModelContract):
        dbt_model = "stg_charges"
        order_id: PrimaryKey
        item_id: PrimaryKey

    resolved = resolve_contracts(_CHARGES)
    (fact,) = resolved.key_facts
    assert fact.value == CandidateKeySet.of(frozenset({"order_id", "item_id"}))


def test_foreign_key_resolves_to_an_edge() -> None:
    manifest = _manifest(_node("model.shop.stg_charges"), _node("model.shop.dim_customers"))

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        customer_id: ForeignKey("dim_customers.customer_id")

    resolved = resolve_contracts(manifest)
    assert resolved.issues == ()
    (edge,) = resolved.foreign_keys
    assert edge == ForeignKeyEdge(
        child=ColumnRef(_CHARGES_SRC, "customer_id"),
        parent=ColumnRef(SourceRef(SourceKind.MODEL, "model.shop.dim_customers"), "customer_id"),
    )


def test_unresolvable_foreign_key_target_is_a_finding() -> None:
    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        customer_id: ForeignKey("dim_custmers.customer_id")

    resolved = resolve_contracts(_CHARGES)
    (issue,) = resolved.issues
    assert issue.code is IssueCode.UNRESOLVED_FOREIGN_KEY
    assert resolved.foreign_keys == ()


# --- Field constraints ----------------------------------------------------------------


def test_constraints_attach_to_the_magnitude_column() -> None:
    class FctOrders(ModelContract):
        dbt_model = "stg_charges"
        order_total: Money(amount="order_total", currency=Currency.USD) = Field(ge=0)

    resolved = resolve_contracts(_CHARGES)
    (constraint,) = resolved.constraints
    assert constraint.column == ColumnRef(_CHARGES_SRC, "order_total")
    assert constraint.constraints.ge == 0


# --- discoverers -----------------------------------------------------------------------


def test_discoverers_satisfy_the_substrate_protocol() -> None:
    assert isinstance(contract_tag_discoverer(), FactDiscoverer)
    assert isinstance(contract_key_discoverer(), FactDiscoverer)


def test_contract_keys_merge_with_dbt_test_sourced_keys() -> None:
    manifest = _manifest(
        _node("model.shop.stg_charges"),
        _unique_test("test.shop.u", column="charge_id", target="model.shop.stg_charges"),
    )

    class StgCharges(ModelContract):
        dbt_model = "stg_charges"
        order_id: PrimaryKey

    name_to_source: Mapping[str, SourceRef] = {}
    buckets = collect(
        manifest,
        (unique_test_discoverer(), contract_key_discoverer()),
        name_to_source=name_to_source,
    )
    values = {f.value for f in buckets[_CHARGES_SRC]}
    assert values == {
        CandidateKeySet.of(frozenset({"charge_id"})),
        CandidateKeySet.of(frozenset({"order_id"})),
    }


def test_unresolved_contracts_contribute_no_facts_through_the_discoverer() -> None:
    class Typo(ModelContract):
        dbt_model = "no_such_model"
        amount: Money(currency=Currency.USD)

    name_to_source: Mapping[str, SourceRef] = {}
    assert collect(_CHARGES, (contract_tag_discoverer(),), name_to_source=name_to_source) == {}
