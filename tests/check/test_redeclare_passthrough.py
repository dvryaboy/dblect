# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract's field annotations use the domain-type DSL (``Money.columns(...)``), a value
# expression pyright cannot read as a type; ``test_run_check`` waives the rule the same way.
"""Re-declaring a per-row-bound type on a model that passes the columns through (issue #329).

A ``Money`` amount binds to its ``currency`` column. Declaring the same type on a staging
model and again on the model reading it is a restatement when the second model's currency
is a plain copy of the first's, and a real conflict when it is anything else.
"""

from __future__ import annotations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFindingKind, run_check
from dblect.demo import Currency, Money
from dblect.manifest import Manifest, ResourceType
from dblect.types import ModelContract
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_DUCKDB = profile_for_adapter("duckdb")

_FCT = "model.shop.fct_payments"
_REPORT = "model.shop.report"


def _manifest_of(fct_sql: str) -> Manifest:
    return _manifest(
        _node(
            "source.shop.raw.payments",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount_cents="BIGINT", currency="VARCHAR"),
        ),
        _node(
            "source.shop.raw.rates",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(currency="VARCHAR", rate="DECIMAL"),
        ),
        _node(
            "model.shop.stg_payments",
            sql="SELECT amount_cents, currency FROM payments",
            columns=_cols(amount_cents="BIGINT", currency="VARCHAR"),
        ),
        _node(
            _FCT,
            sql=fct_sql,
            columns=_cols(amount_cents="BIGINT", currency="VARCHAR"),
        ),
        _node(
            _REPORT,
            sql="SELECT amount_cents, currency FROM fct_payments",
            columns=_cols(amount_cents="BIGINT", currency="VARCHAR"),
        ),
    )


def _declare(model: str, annotation: object) -> None:
    # Built through the metaclass: the parametrized type cannot be read from a class
    # body under postponed evaluation.
    type(
        f"Contract_{model}",
        (ModelContract,),
        {"dbt_model": model, "__annotations__": {"amount_cents": annotation}},
    )


_USD = Money.refine(currency=Currency.USD).columns(amount="amount_cents")
_EUR = Money.refine(currency=Currency.EUR).columns(amount="amount_cents")
_PER_ROW = Money.columns(amount="amount_cents", currency="currency")
_PASS_THROUGH = "SELECT amount_cents, currency FROM stg_payments"


def _contradictions(
    fct_sql: str, *, staging: object = _PER_ROW, fact: object = _PER_ROW
) -> dict[str | None, str]:
    """The contradiction message of each model that has one."""
    _declare("stg_payments", staging)
    _declare("fct_payments", fact)
    report = run_check(_manifest_of(fct_sql), _DUCKDB)
    return {
        f.model_unique_id: f.message
        for f in report.findings
        if f.kind is CheckFindingKind.DOMAIN_TYPE_CONTRADICTION
    }


def _contradicted(
    fct_sql: str, *, staging: object = _PER_ROW, fact: object = _PER_ROW
) -> set[str | None]:
    return set(_contradictions(fct_sql, staging=staging, fact=fact))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT amount_cents, currency FROM stg_payments",
        "SELECT amount_cents, (currency) AS currency FROM stg_payments",
        "SELECT amount_cents, CAST(currency AS VARCHAR) AS currency FROM stg_payments",
        "WITH s AS (SELECT * FROM stg_payments) SELECT amount_cents, currency FROM s",
    ],
)
def test_a_plain_copy_of_the_companion_is_a_restatement(sql: str) -> None:
    assert _contradicted(sql) == set()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT amount_cents, UPPER(currency) AS currency FROM stg_payments",
        "SELECT amount_cents, COALESCE(currency, 'USD') AS currency FROM stg_payments",
        "SELECT p.amount_cents, r.currency "
        "FROM stg_payments AS p JOIN rates AS r ON r.rate = p.amount_cents",
    ],
)
def test_a_companion_that_is_not_a_plain_copy_still_contradicts(sql: str) -> None:
    assert _FCT in _contradicted(sql)


@pytest.mark.parametrize(
    ("staging", "fact", "contradicts"),
    [
        (_USD, _USD, False),
        (_USD, _EUR, True),
        (_PER_ROW, _EUR, True),
    ],
)
def test_a_pinned_facet_decides_the_same_as_before(
    staging: object, fact: object, contradicts: bool
) -> None:
    found = _FCT in _contradicted(_PASS_THROUGH, staging=staging, fact=fact)
    assert found is contradicts


# --- the message names both types and what differs (#330) -------------------------


def test_a_unit_conflict_names_both_units() -> None:
    message = _contradictions(_PASS_THROUGH, staging=_USD, fact=_EUR)[_FCT]
    assert "usd" in message
    assert "eur" in message
    assert "unit" in message


def test_a_pinned_unit_against_a_per_row_one_names_both() -> None:
    message = _contradictions(_PASS_THROUGH, staging=_PER_ROW, fact=_EUR)[_FCT]
    assert "eur" in message
    assert "per-row currency" in message


def test_a_companion_that_differs_only_in_identity_names_both_columns() -> None:
    sql = "SELECT amount_cents, UPPER(currency) AS currency FROM stg_payments"
    message = _contradictions(sql)[_FCT]
    assert "companion binding" in message
    assert "fct_payments.currency" in message
    assert "stg_payments.currency" in message


def test_a_model_that_only_inherits_the_conflict_says_it_carries_the_type() -> None:
    message = _contradictions(_PASS_THROUGH, staging=_USD, fact=_EUR)[_REPORT]
    assert "carries" in message
    assert "eur" in message
