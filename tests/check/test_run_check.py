# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract method's ``self`` is a ContractSelf proxy at capture, not a real
# instance; annotating it that way trips pyright's self-supertype rule while keeping
# the proxy usage checked. Typed ``self`` in authored contracts is the stubs concern.
"""``run_check``: resolve contracts, propagate, derive the domain-type findings.

The three the demo promises, all driven by declarations: a contract that does not
resolve against the manifest is a finding; a declared type contradicted by what
flows down the DAG is the currency-creep finding (and it reaches models nobody
declared); and a sum over a per-row companion that nothing holds constant is the
mixed-currency-sum finding, which a declared dependency discharges. A consistent
single-currency project stays quiet.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFindingKind, run_check
from dblect.contracts import ContractSelf, contract
from dblect.demo import Currency, Money
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import Column
from dblect.types import ModelContract

_DUCKDB = profile_for_adapter("duckdb")

MoneyUSD = Money.refine(currency=Currency.USD)


def _cols(**types: str) -> Mapping[str, Column]:
    return {n: Column(name=n, data_type=t, description=None) for n, t in types.items()}


def _node(uid: str, *, kind: ResourceType, sql: str | None, columns: Mapping[str, Column]) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=kind,
        fqn=tuple(uid.split(".")[1:]),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=f"models/{uid.split('.')[-1]}.sql",
        columns=columns,
    )


def _kinds(report: object) -> list[CheckFindingKind]:
    assert hasattr(report, "findings")
    return [f.kind for f in report.findings]  # type: ignore[attr-defined]


# --- contract issues ------------------------------------------------------------


def test_unresolved_contract_is_a_finding() -> None:
    class Ghost(ModelContract):
        dbt_model = "does_not_exist"
        amount: MoneyUSD

    manifest = Manifest(schema_version="v12", adapter_type="duckdb", nodes={})
    report = run_check(manifest, _DUCKDB)
    assert _kinds(report) == [CheckFindingKind.CONTRACT_ISSUE]


def test_a_model_that_cannot_be_analyzed_is_surfaced() -> None:
    # A model whose SQL does not parse must not vanish into a clean report: it is
    # reported as unbuilt so the absence of findings on it is never read as "clean".
    broken = _node(
        "model.shop.broken",
        kind=ResourceType.MODEL,
        sql="select from where group",  # not valid SQL
        columns=_cols(x="INT"),
    )
    manifest = Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={broken.unique_id: broken}
    )
    report = run_check(manifest, _DUCKDB)
    assert [m.unique_id for m in report.unbuilt] == ["model.shop.broken"]
    assert report.models_analyzed == 0


# --- currency creep (a declared type contradicted downstream) --------------------

_CREEP_NODES = (
    _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(amount="DECIMAL", currency="VARCHAR"),
    ),
    _node(
        "model.shop.stg_payments",
        kind=ResourceType.MODEL,
        sql="SELECT amount FROM payments",
        columns=_cols(amount="DECIMAL"),
    ),
    _node(
        "model.shop.orders",
        kind=ResourceType.MODEL,
        sql="SELECT amount FROM stg_payments",
        columns=_cols(amount="DECIMAL"),
    ),
)


def _creep_manifest() -> Manifest:
    return Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in _CREEP_NODES},
    )


def test_currency_creep_flags_the_contradiction_and_its_blast_radius() -> None:
    class RawPayments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD  # declared single-currency, contradicted by the multi-currency source

    report = run_check(_creep_manifest(), _DUCKDB)
    contradictions = [
        f for f in report.findings if f.kind is CheckFindingKind.DOMAIN_TYPE_CONTRADICTION
    ]
    flagged = {f.model_unique_id for f in contradictions}
    assert "model.shop.stg_payments" in flagged
    assert "model.shop.orders" in flagged  # the blast radius reaches an undeclared model


def test_consistent_single_currency_is_quiet() -> None:
    class RawPayments(ModelContract):
        dbt_model = "payments"
        amount: MoneyUSD

    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD

    report = run_check(_creep_manifest(), _DUCKDB)
    assert report.findings == ()


# --- mixed-currency sum ---------------------------------------------------------

_AGG_NODES = (
    _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(amount="DECIMAL", currency="VARCHAR", country="VARCHAR"),
    ),
    _node(
        "model.shop.revenue_by_country",
        kind=ResourceType.MODEL,
        sql="SELECT country, SUM(amount) AS total FROM payments GROUP BY country",
        columns=_cols(country="VARCHAR", total="DECIMAL"),
    ),
)


def _agg_manifest() -> Manifest:
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in _AGG_NODES}
    )


def test_mixed_currency_sum_is_flagged() -> None:
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(_agg_manifest(), _DUCKDB)
    agg = [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    assert len(agg) == 1
    assert agg[0].model_unique_id == "model.shop.revenue_by_country"
    assert agg[0].column == "total"


def test_sum_over_a_case_expression_is_not_flagged() -> None:
    # `sum(CASE WHEN ... THEN amount ELSE 0 END)` clears to naked at the CASE (it
    # mixes money with a dimensionless 0), which is a different concern than a
    # companion that is not constant per group. The aggregation finding is reserved
    # for a reduction directly over a tagged column, so this stays quiet.
    nodes = (
        _node(
            "source.shop.raw.payments",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount="DECIMAL", currency="VARCHAR", method="VARCHAR", k="VARCHAR"),
        ),
        _node(
            "model.shop.by_method",
            kind=ResourceType.MODEL,
            sql=(
                "SELECT k, SUM(CASE WHEN method = 'card' THEN amount ELSE 0 END) AS card "
                "FROM payments GROUP BY k"
            ),
            columns=_cols(k="VARCHAR", card="DECIMAL"),
        ),
    )
    manifest = Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )

    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(manifest, _DUCKDB)
    assert not [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]


def test_declared_dependency_discharges_the_sum() -> None:
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

        @contract
        def country_sets_currency(self: ContractSelf) -> object:
            return self.country.determines(self.currency)

    report = run_check(_agg_manifest(), _DUCKDB)
    assert not [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]


# --- coverage -------------------------------------------------------------------


def test_resolution_and_grounding_coverage_are_reported() -> None:
    class RawPayments(ModelContract):
        dbt_model = "payments"
        amount: MoneyUSD

    class StgPayments(ModelContract):
        dbt_model = "stg_payments"
        amount: MoneyUSD

    report = run_check(_creep_manifest(), _DUCKDB)

    res = report.resolution
    assert res.resolved_columns > 0
    assert res.fraction == 1.0
    assert res.unexpanded_stars == 0

    # Both contracts name a column whose lineage resolves, so every named column is
    # checkable: a declared type that actually reaches the propagator.
    grounding = report.grounding
    assert grounding.contract_columns >= 1
    assert grounding.contract_columns_checkable == grounding.contract_columns
    dt = next(p for p in grounding.by_property if p.property_name == "domain_type")
    assert dt.grounded >= 1


def test_resolution_floor_fires_on_blindness_and_is_silent_when_clean() -> None:
    # `SELECT *` over a source with no documented columns cannot be expanded, so
    # the model's one output column is an unexpanded-star blind site: resolution 0%.
    opaque = _node("source.shop.raw.opaque", kind=ResourceType.SOURCE, sql=None, columns=_cols())
    passthru = _node(
        "model.shop.passthru",
        kind=ResourceType.MODEL,
        sql="SELECT * FROM opaque",
        columns=_cols(x="INT"),
    )
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={opaque.unique_id: opaque, passthru.unique_id: passthru},
    )

    clean = run_check(manifest, _DUCKDB)
    # The star is a blind site, not a build failure, and nothing trips at 0%
    # resolution until a floor is set.
    assert passthru.unique_id not in {m.unique_id for m in clean.unbuilt}
    assert clean.resolution.fraction == 0.0
    assert CheckFindingKind.RESOLUTION_BELOW_FLOOR not in _kinds(clean)

    breached = run_check(manifest, _DUCKDB, resolution_floor=0.5)
    assert CheckFindingKind.RESOLUTION_BELOW_FLOOR in _kinds(breached)


def test_resolution_floor_is_silent_when_full_coverage_clears_it() -> None:
    # No contracts: the floor keys on resolution alone, and every column in this
    # manifest resolves, so even a near-1.0 floor stays silent.
    report = run_check(_creep_manifest(), _DUCKDB, resolution_floor=0.99)
    assert report.resolution.fraction == 1.0
    assert CheckFindingKind.RESOLUTION_BELOW_FLOOR not in _kinds(report)
