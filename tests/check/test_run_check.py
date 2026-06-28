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

import pytest

from dblect.adapters import profile_for_adapter
from dblect.audit import SpanBasis
from dblect.check import CheckFindingKind, run_check
from dblect.contracts import ContractSelf, contract
from dblect.demo import Currency, Money
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import Column
from dblect.types import IssueCode, ModelContract

_DUCKDB = profile_for_adapter("duckdb")

MoneyUSD = Money.refine(currency=Currency.USD)
MoneyEUR = Money.refine(currency=Currency.EUR)


def _cols(**types: str) -> Mapping[str, Column]:
    return {n: Column(name=n, data_type=t, description=None) for n, t in types.items()}


def _node(
    uid: str,
    *,
    kind: ResourceType,
    sql: str | None,
    columns: Mapping[str, Column],
    raw: str | None = None,
) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=kind,
        fqn=tuple(uid.split(".")[1:]),
        package_name="shop",
        schema="analytics",
        raw_code=raw,
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


def test_contract_issue_findings_carry_their_distinct_cause_codes() -> None:
    # Each contract issue pins its specific cause, not just the shared bucket, so a
    # consumer can tell one cause from another. Two distinct causes in one run prove
    # the code tracks the issue rather than a single hardcoded value, observed through
    # the public check boundary. One model name resolves to two nodes (ambiguous), the
    # other to none (unresolved).
    orders = _node(
        "model.shop.orders", kind=ResourceType.MODEL, sql="SELECT 1 AS x", columns=_cols(x="INT")
    )
    orders_dupe = _node(
        "model.other.orders", kind=ResourceType.MODEL, sql="SELECT 1 AS x", columns=_cols(x="INT")
    )
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={orders.unique_id: orders, orders_dupe.unique_id: orders_dupe},
    )

    class Ambiguous(ModelContract):
        dbt_model = "orders"
        amount: MoneyUSD

    class Ghost(ModelContract):
        dbt_model = "does_not_exist"
        amount: MoneyUSD

    report = run_check(manifest, _DUCKDB)
    assert {f.code for f in report.findings} == {
        IssueCode.AMBIGUOUS_MODEL,
        IssueCode.UNRESOLVED_MODEL,
    }
    assert all(f.kind is CheckFindingKind.CONTRACT_ISSUE for f in report.findings)


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


def test_aggregation_finding_back_maps_to_the_source_line() -> None:
    # Two build-prelude lines in the compiled SQL push the SUM down from where the
    # developer wrote it. The finding's compiled span is what the parser saw; its
    # reported span is back-mapped onto the template, so it points at the SUM line in
    # the `.sql`, not the compiled-relative line.
    raw = "select\n  country,\n  sum(amount) as total\nfrom payments\ngroup by country"
    compiled = (
        "-- generated by dbt\n"
        "-- compiled at build\n"
        "select\n  country,\n  sum(amount) as total\nfrom payments\ngroup by country"
    )
    mart = _node(
        "model.shop.revenue_by_country",
        kind=ResourceType.MODEL,
        sql=compiled,
        raw=raw,
        columns=_cols(country="VARCHAR", total="DECIMAL"),
    )
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={_AGG_NODES[0].unique_id: _AGG_NODES[0], mart.unique_id: mart},
    )

    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(manifest, _DUCKDB)
    [agg] = [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    span = agg.located_span
    assert span.basis is SpanBasis.SOURCE
    # The compiled span sits below the source line by the two-line prelude; the back-map
    # undoes that and lands on the SUM the developer wrote.
    assert agg.line_start > span.line_start
    assert "sum" in raw.splitlines()[span.line_start - 1].lower()


def test_noqa_on_the_source_line_suppresses_a_macro_shifted_aggregation() -> None:
    # The same two-line prelude shifts the SUM to compiled line 5 while the developer
    # wrote it on source line 3. A `-- noqa` on the source line silences the finding,
    # because suppression matches the back-mapped source span, not the compiled line two
    # rows below it. The directive rides through compilation verbatim onto the SUM line.
    noqa = "  -- noqa: DBLECT_AGGREGATION_NOT_WELL_TYPED"
    raw = f"select\n  country,\n  sum(amount) as total{noqa}\nfrom payments\ngroup by country"
    compiled = (
        "-- generated by dbt\n"
        "-- compiled at build\n"
        f"select\n  country,\n  sum(amount) as total{noqa}\nfrom payments\ngroup by country"
    )
    mart = _node(
        "model.shop.revenue_by_country",
        kind=ResourceType.MODEL,
        sql=compiled,
        raw=raw,
        columns=_cols(country="VARCHAR", total="DECIMAL"),
    )
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={_AGG_NODES[0].unique_id: _AGG_NODES[0], mart.unique_id: mart},
    )

    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(manifest, _DUCKDB)
    assert not [
        f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED
    ], "the noqa on the source SUM line should silence the aggregation finding"
    [hidden] = [
        s
        for s in report.suppressed
        if s.finding.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED
    ]
    # Directive on source line 3; the compiled SUM line is 5, two rows past the
    # one-line "directive above" slack, so only the source-span match suppresses it.
    assert hidden.directive_line == 3
    assert hidden.finding.line_start == 5
    assert hidden.finding.located_span.basis is SpanBasis.SOURCE


def test_noqa_on_the_macro_call_line_suppresses_a_macro_emitted_aggregation() -> None:
    # The SUM that the algebra cannot call well typed is emitted by `{{ revenue() }}`, so
    # it has no source line of its own: its compiled line back-maps to the macro call site
    # (source line 3). A `-- noqa` the developer placed on that call line silences it, the
    # declaration-family counterpart of the macro-emitted structural case.
    noqa = "  -- noqa: DBLECT_AGGREGATION_NOT_WELL_TYPED"
    raw = f"select\n  country,\n  {{{{ revenue() }}}}{noqa}\nfrom payments\ngroup by country"
    compiled = "select\n  country,\n  sum(amount) as total\nfrom payments\ngroup by country"
    mart = _node(
        "model.shop.revenue_by_country",
        kind=ResourceType.MODEL,
        sql=compiled,
        raw=raw,
        columns=_cols(country="VARCHAR", total="DECIMAL"),
    )
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={_AGG_NODES[0].unique_id: _AGG_NODES[0], mart.unique_id: mart},
    )

    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(manifest, _DUCKDB)
    assert not [
        f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED
    ], "the noqa on the macro call line should silence the emitted aggregation finding"
    [hidden] = [
        s
        for s in report.suppressed
        if s.finding.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED
    ]
    # The SUM is at compiled line 3; it anchors to the `{{ revenue() }}` call at source
    # line 3, where the directive sits.
    assert hidden.directive_line == 3
    assert hidden.finding.line_start == 3
    assert hidden.finding.located_span.basis is SpanBasis.MACRO_CALL
    assert (hidden.finding.located_span.line_start, hidden.finding.located_span.line_end) == (3, 3)


def test_mixed_currency_sum_is_flagged() -> None:
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(_agg_manifest(), _DUCKDB)
    agg = [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    assert len(agg) == 1
    assert agg[0].model_unique_id == "model.shop.revenue_by_country"
    assert agg[0].column == "total"


def test_mixed_currency_sum_message_names_the_columns() -> None:
    # The finding names what the coherence guard reasoned about: the aggregate and
    # the column it reduced, the per-row companion that varies, and the grouping that
    # fails to hold it constant, so a reader can act without opening the model (#109).
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(_agg_manifest(), _DUCKDB)
    [agg] = [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    assert "amount" in agg.message  # the reduced column
    assert "currency" in agg.message  # the per-row companion that varies
    assert "country" in agg.message  # the grouping that does not hold it constant


def _one_agg_manifest(sql_fn: str) -> Manifest:
    nodes = (
        _node(
            "source.shop.raw.payments",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount="DECIMAL", currency="VARCHAR", country="VARCHAR"),
        ),
        _node(
            "model.shop.agg",
            kind=ResourceType.MODEL,
            sql=f"SELECT country, {sql_fn}(amount) AS v FROM payments GROUP BY country",
            columns=_cols(country="VARCHAR", v="DECIMAL"),
        ),
    )
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


# Two non-sum representatives: `avg` is a different sqlglot node than `sum`, and `median`
# a structurally distinct one, enough to confirm the path fires and the message renders
# the actual aggregate rather than a hard-coded "sum". The full breadth of which
# aggregates combine is pinned cheaply at the classification boundary in
# tests/sql/test_aggregates.py, so it is not re-run through the check pipeline here.
@pytest.mark.parametrize("sql_fn", ["AVG", "MEDIAN"])
def test_combining_aggregates_over_mixed_currency_are_flagged(sql_fn: str) -> None:
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(_one_agg_manifest(sql_fn), _DUCKDB)
    [agg] = [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    assert "currency" in agg.message
    assert sql_fn.lower() in agg.message


def test_counting_aggregate_is_not_flagged() -> None:
    # Counting money is always well typed: count ignores values, so currency is
    # irrelevant. Only the combining class carries the obligation.
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(_one_agg_manifest("COUNT"), _DUCKDB)
    assert not [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]


def test_selecting_aggregate_is_not_flagged_under_lenient() -> None:
    # min/max return a real input value rather than synthesizing one, so under the lenient
    # default they do not raise the not-well-typed finding (their result tag widens to top
    # and is caught later where a definite tag is required). An eager finding for the
    # tag-blind comparison is the strict-mode question, tracked separately (#116).
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(_one_agg_manifest("MIN"), _DUCKDB)
    assert not [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]


def test_collection_aggregate_over_a_tagged_column_is_not_flagged() -> None:
    # array_agg gathers values into a list rather than reducing a magnitude, so it carries
    # no currency obligation and is left unclassified (lenient).
    nodes = (
        _node(
            "source.shop.raw.payments",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount="DECIMAL", currency="VARCHAR", country="VARCHAR"),
        ),
        _node(
            "model.shop.amounts_by_country",
            kind=ResourceType.MODEL,
            sql="SELECT country, array_agg(amount) AS amounts FROM payments GROUP BY country",
            columns=_cols(country="VARCHAR", amounts="DECIMAL[]"),
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


def test_sum_over_a_scaled_amount_is_flagged() -> None:
    # `sum(amount * 2)` keeps the per-row currency through the scalar factor, so the
    # reduction is just as not-well-typed as `sum(amount)`. The signal flags it because
    # the operand still carries a live companion; the earlier bare-column restriction
    # dropped this common shape (`amount * rate`, `price * quantity`) and the recall is
    # back now that the check reads the guard's clear rather than the operand's shape.
    nodes = (
        _node(
            "source.shop.raw.payments",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount="DECIMAL", currency="VARCHAR", country="VARCHAR"),
        ),
        _node(
            "model.shop.scaled",
            kind=ResourceType.MODEL,
            sql="SELECT country, SUM(amount * 2) AS v FROM payments GROUP BY country",
            columns=_cols(country="VARCHAR", v="DECIMAL"),
        ),
    )
    manifest = Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )

    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(manifest, _DUCKDB)
    [agg] = [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    assert agg.column == "v"
    assert "currency" in agg.message


def test_sum_grouped_by_a_computed_key_is_flagged() -> None:
    # `GROUP BY date_trunc('month', created_at)` is a real grouping the builder cannot
    # resolve to plain columns (a computed key), so the site stamps `group_refs=None`.
    # The currency companion is no more held by an opaque group than by a resolved one
    # that omits it, and the guard records the clear "rather than guessing" it is safe,
    # so the reduction is flagged. Only a windowed aggregate (an unstamped site) is the
    # deferred case; an opaque GROUP BY is not windowed.
    nodes = (
        _node(
            "source.shop.raw.payments",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount="DECIMAL", currency="VARCHAR", created_at="TIMESTAMP"),
        ),
        _node(
            "model.shop.monthly",
            kind=ResourceType.MODEL,
            sql=(
                "SELECT date_trunc('month', created_at) AS m, SUM(amount) AS total "
                "FROM payments GROUP BY date_trunc('month', created_at)"
            ),
            columns=_cols(m="TIMESTAMP", total="DECIMAL"),
        ),
    )
    manifest = Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )

    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

    report = run_check(manifest, _DUCKDB)
    [agg] = [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    assert agg.column == "total"
    assert "currency" in agg.message


def test_declared_dependency_discharges_the_sum() -> None:
    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")

        @contract
        def country_sets_currency(self: ContractSelf) -> object:
            return self.country.determines(self.currency)

    report = run_check(_agg_manifest(), _DUCKDB)
    assert not [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]


# --- join-key type compatibility (C2) -------------------------------------------


def _join_keys_manifest() -> Manifest:
    # The join key is named ``amount`` because ``MoneyUSD`` binds its magnitude to the
    # field column ``amount``; the key being a money value is what makes the currency
    # mismatch a domain-type conflict.
    nodes = (
        _node(
            "source.shop.raw.usd_ledger",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount="DECIMAL"),
        ),
        _node(
            "source.shop.raw.eur_ledger",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount="DECIMAL"),
        ),
        _node(
            "model.shop.reconciled",
            kind=ResourceType.MODEL,
            sql=(
                "SELECT u.amount AS amount FROM usd_ledger AS u "
                "JOIN eur_ledger AS e ON u.amount = e.amount"
            ),
            columns=_cols(amount="DECIMAL"),
        ),
    )
    return Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )


def test_join_on_incompatible_domain_types_is_flagged() -> None:
    # Equating a USD-pinned key against a EUR-pinned one joins values that cannot mean
    # the same thing: the two tags meet to a conflict. The check reads that off the ON
    # clause and reports it, the join-key counterpart of the not-well-typed reduction.
    class UsdLedger(ModelContract):
        dbt_model = "usd_ledger"
        amount: MoneyUSD

    class EurLedger(ModelContract):
        dbt_model = "eur_ledger"
        amount: MoneyEUR

    report = run_check(_join_keys_manifest(), _DUCKDB)
    [jk] = [f for f in report.findings if f.kind is CheckFindingKind.JOIN_KEY_TYPE_MISMATCH]
    assert jk.model_unique_id == "model.shop.reconciled"
    assert "amount" in jk.message


def test_join_on_compatible_domain_types_is_quiet() -> None:
    # Both keys USD: the tags agree, so the equality is well typed and nothing fires.
    class UsdLedger(ModelContract):
        dbt_model = "usd_ledger"
        amount: MoneyUSD

    class EurLedger(ModelContract):
        dbt_model = "eur_ledger"
        amount: MoneyUSD

    report = run_check(_join_keys_manifest(), _DUCKDB)
    assert not [f for f in report.findings if f.kind is CheckFindingKind.JOIN_KEY_TYPE_MISMATCH]


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


# --- declaration-level suppression (`-- noqa`) ----------------------------------
#
# A team that genuinely accepts a flagged mixed-currency sum acknowledges it in the
# model the way the structural family already does, and the finding is silenced with
# the acknowledgement visible in review.


def _agg_manifest_with_source(model_sql: str) -> Manifest:
    """The mixed-currency-sum manifest, with the aggregating model's SQL supplied so a
    suppression comment can be threaded onto a known line. ``raw_code`` carries the
    directive (where the developer writes it); ``compiled_code`` is what the analysis
    parses, kept identical so line numbers line up the way they do for a model with no
    macro expansion."""
    payments = _node(
        "source.shop.raw.payments",
        kind=ResourceType.SOURCE,
        sql=None,
        columns=_cols(amount="DECIMAL", currency="VARCHAR", country="VARCHAR"),
    )
    model = Node(
        unique_id="model.shop.revenue_by_country",
        name="revenue_by_country",
        resource_type=ResourceType.MODEL,
        fqn=("shop", "revenue_by_country"),
        package_name="shop",
        schema="analytics",
        raw_code=model_sql,
        compiled_code=model_sql,
        original_file_path="models/revenue_by_country.sql",
        columns=_cols(country="VARCHAR", total="DECIMAL"),
    )
    return Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={payments.unique_id: payments, model.unique_id: model},
    )


def _register_payments_contract() -> None:
    """Declare ``amount`` as multi-currency money so the mixed-currency sum fires.
    Defined per test because a contract registers on class definition, into the
    per-test isolated registry the conftest provides."""

    class Payments(ModelContract):
        dbt_model = "payments"
        amount: Money.columns(amount="amount", currency="currency")


_MIXED_SUM_WITH_NOQA = (
    "SELECT country,\n"
    "  SUM(amount) AS total -- noqa: DBLECT_AGGREGATION_NOT_WELL_TYPED\n"
    "FROM payments GROUP BY country"
)
_MIXED_SUM_BARE_NOQA = (
    "SELECT country,\n  SUM(amount) AS total -- noqa\nFROM payments GROUP BY country"
)
_MIXED_SUM_NO_NOQA = "SELECT country,\n  SUM(amount) AS total\nFROM payments GROUP BY country"


def test_check_finding_carries_line_provenance() -> None:
    _register_payments_contract()
    report = run_check(_agg_manifest_with_source(_MIXED_SUM_NO_NOQA), _DUCKDB)
    [agg] = [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    # The SUM sits on the second line, so the finding points there rather than at the
    # whole model. A line of 0 would mean "could not locate" and leave it unsuppressible.
    assert agg.line_start == 2
    assert agg.line_end >= agg.line_start


def test_check_finding_with_matching_code_is_suppressed() -> None:
    _register_payments_contract()
    report = run_check(_agg_manifest_with_source(_MIXED_SUM_WITH_NOQA), _DUCKDB)
    assert not [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    [s] = [
        s
        for s in report.suppressed
        if s.finding.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED
    ]
    assert not s.bare


def test_check_finding_with_bare_noqa_is_suppressed() -> None:
    _register_payments_contract()
    report = run_check(_agg_manifest_with_source(_MIXED_SUM_BARE_NOQA), _DUCKDB)
    assert not [f for f in report.findings if f.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED]
    [s] = [
        s
        for s in report.suppressed
        if s.finding.kind is CheckFindingKind.AGGREGATION_NOT_WELL_TYPED
    ]
    assert s.bare
