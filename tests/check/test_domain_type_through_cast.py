# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
# A contract's field annotations use the domain-type DSL (``Money.columns(...)``), a value
# expression pyright cannot read as a type; ``test_run_check`` waives the rule the same way.
"""A type declared at a source reaches the marts through a staging cast (issue #316).

Staging models routinely cast every column. A money column typed at the source must still
be money after ``cast(amount_cents as bigint)``, so the mart's sum over a per-row currency
is still reported; cast to text it is no longer a magnitude, so the finding goes quiet.
An entity id keeps its identity through int and string casts and loses it into a date.
"""

from __future__ import annotations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFindingKind, run_check
from dblect.demo import Money
from dblect.manifest import Manifest, ResourceType
from dblect.types import DomainType, Integer, ModelContract, NominalEnum
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_DUCKDB = profile_for_adapter("duckdb")


def _sales_manifest(stg_expr: str) -> Manifest:
    return _manifest(
        _node(
            "source.shop.raw.sales",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(amount_cents="BIGINT", currency_code="VARCHAR", ds="DATE"),
        ),
        _node(
            "model.shop.stg_sales",
            sql=f"SELECT ds, currency_code, {stg_expr} AS amount_cents FROM sales",
            columns=_cols(ds="DATE", currency_code="VARCHAR", amount_cents="BIGINT"),
        ),
        _node(
            "model.shop.daily",
            sql="SELECT ds, SUM(amount_cents) AS total FROM stg_sales GROUP BY ds",
            columns=_cols(ds="DATE", total="BIGINT"),
        ),
    )


def _declare_sales() -> None:
    class RawSales(ModelContract):
        dbt_model = "sales"
        amount_cents: Money.columns(amount="amount_cents", currency="currency_code")


def _kinds(manifest: Manifest) -> list[CheckFindingKind]:
    return [f.kind for f in run_check(manifest, _DUCKDB).findings]


_NUMERIC_CASTS = [
    "cast(amount_cents as bigint)",
    "cast(amount_cents as integer)",
    "cast(amount_cents as decimal(18, 2))",
    "cast(amount_cents as double)",
    "try_cast(amount_cents as decimal(10, 0))",
    "amount_cents::decimal(38, 9)",
]
_NON_NUMERIC_CASTS = [
    "cast(amount_cents as varchar)",
    "try_cast(amount_cents as varchar)",
    "amount_cents::varchar",
    "cast(amount_cents as date)",
    "cast(amount_cents as boolean)",
    "cast(amount_cents as json)",
]


def test_the_uncast_baseline_reports_the_mixed_currency_sum() -> None:
    _declare_sales()
    assert CheckFindingKind.AGGREGATION_NOT_WELL_TYPED in _kinds(_sales_manifest("amount_cents"))


@pytest.mark.parametrize("expr", _NUMERIC_CASTS)
def test_a_numeric_cast_keeps_the_money_type_so_the_sum_is_reported(expr: str) -> None:
    _declare_sales()
    assert CheckFindingKind.AGGREGATION_NOT_WELL_TYPED in _kinds(_sales_manifest(expr))


@pytest.mark.parametrize("expr", _NON_NUMERIC_CASTS)
def test_a_non_numeric_cast_makes_no_claim_so_the_sum_is_quiet(expr: str) -> None:
    _declare_sales()
    assert CheckFindingKind.AGGREGATION_NOT_WELL_TYPED not in _kinds(_sales_manifest(expr))


# --- identifiers -------------------------------------------------------------------


class _Entity(NominalEnum):
    PLAYER = "player"
    TEAM = "team"


class _IntId(DomainType):
    id: Integer
    entity: _Entity


def _id_join_manifest(stg_expr: str) -> Manifest:
    return _manifest(
        _node(
            "source.shop.raw.player",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(id="INT"),
        ),
        _node(
            "source.shop.raw.team",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(id="INT"),
        ),
        _node(
            "model.shop.stg_player",
            sql=f"SELECT {stg_expr} AS id FROM player",
            columns=_cols(id="INT"),
        ),
        _node(
            "model.shop.joined",
            sql="SELECT p.id AS pid FROM stg_player AS p JOIN team AS t ON p.id = t.id",
            columns=_cols(pid="INT"),
        ),
    )


def _declare_ids() -> None:
    class Player(ModelContract):
        dbt_model = "player"
        id: _IntId.refine(entity=_Entity.PLAYER)

    class Team(ModelContract):
        dbt_model = "team"
        id: _IntId.refine(entity=_Entity.TEAM)


@pytest.mark.parametrize(
    ("expr", "keeps"),
    [
        ("id", True),
        ("cast(id as bigint)", True),
        ("cast(id as varchar)", True),
        ("try_cast(id as varchar)", True),
        ("id::varchar", True),
        ("cast(id as date)", False),
        ("cast(id as boolean)", False),
    ],
)
def test_an_identifier_survives_integer_and_string_casts_only(expr: str, keeps: bool) -> None:
    _declare_ids()
    flagged = CheckFindingKind.JOIN_KEY_TYPE_MISMATCH in _kinds(_id_join_manifest(expr))
    assert flagged is keeps
