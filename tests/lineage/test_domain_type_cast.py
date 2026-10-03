"""A CAST keeps or drops a domain tag by what the target type can still mean.

A cast changes storage, not meaning: a money column cast between numeric types is the
same money in the same unit, with the same per-row currency companion. A cast to a
non-numeric type (text, date, json) no longer holds a magnitude, so it makes no claim.
An identifier tag (nominal only, no dimension) is the one case that also survives a
cast to text, because an entity's id is still that entity's id as a string.

The decision is made over the closed space of ``DataType.Type`` (one test per value
through the classifier) and over the tag shapes (one row per shape and target class,
through propagation). Casts spelled ``CAST``, ``TRY_CAST``, ``::`` and BigQuery's
``SAFE_CAST`` parse to the same two node types and must agree.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from sqlglot import expressions as exp

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    CONFLICT,
    NAKED,
    CastTarget,
    Concrete,
    Dimension,
    DomainTag,
    PerRow,
    cast_target,
    domain_type_grounding,
    domain_type_property,
    tagged,
)

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.charges")
_MODEL = SourceRef(SourceKind.MODEL, "model.shop.m")
_DType = exp.DType

_USD = tagged(dimension=Dimension.of(Concrete("usd")))
_PER_ROW = tagged(dimension=Dimension.of(PerRow(ColumnRef(_SRC, "currency"))))
_ENTITY_ID = tagged(nominal={"entity": Concrete("player")})
_MONEY_WITH_NOMINAL = tagged(
    dimension=Dimension.of(Concrete("usd")), nominal={"tax": Concrete("gross")}
)

# --- the closed space of cast targets ---------------------------------------------

_NUMERIC = frozenset(
    {
        _DType.TINYINT, _DType.SMALLINT, _DType.INT, _DType.BIGINT, _DType.MEDIUMINT,
        _DType.INT128, _DType.INT256, _DType.UTINYINT, _DType.USMALLINT, _DType.UINT,
        _DType.UBIGINT, _DType.UMEDIUMINT, _DType.UINT128, _DType.UINT256, _DType.BIGNUM,
        _DType.SERIAL, _DType.SMALLSERIAL, _DType.BIGSERIAL,
        _DType.FLOAT, _DType.DOUBLE, _DType.UDOUBLE, _DType.DECFLOAT,
        _DType.DECIMAL, _DType.DECIMAL32, _DType.DECIMAL64, _DType.DECIMAL128,
        _DType.DECIMAL256, _DType.UDECIMAL, _DType.BIGDECIMAL,
        _DType.MONEY, _DType.SMALLMONEY,
    }
)  # fmt: skip
_TEXT = frozenset(
    {
        _DType.TEXT, _DType.CHAR, _DType.VARCHAR, _DType.NAME, _DType.NVARCHAR, _DType.NCHAR,
        _DType.BPCHAR, _DType.TINYTEXT, _DType.MEDIUMTEXT, _DType.LONGTEXT, _DType.FIXEDSTRING,
    }
)  # fmt: skip


@pytest.mark.parametrize("dtype", list(_DType), ids=[t.name for t in _DType])
def test_every_cast_target_is_classified(dtype: exp.DType) -> None:
    expected = (
        CastTarget.NUMERIC
        if dtype in _NUMERIC
        else CastTarget.TEXT
        if dtype in _TEXT
        else CastTarget.OTHER
    )
    assert cast_target(exp.DataType(this=dtype)) is expected


def test_a_cast_target_that_is_not_a_type_is_other() -> None:
    assert cast_target(exp.Identifier(this="x")) is CastTarget.OTHER
    assert cast_target(None) is CastTarget.OTHER


# --- the transfer, per tag shape and target class ----------------------------------

_TARGET_SQL: Mapping[CastTarget, str] = {
    CastTarget.NUMERIC: "bigint",
    CastTarget.TEXT: "varchar",
    CastTarget.OTHER: "date",
}

# (tag, expected per target class). A magnitude survives numeric targets only; an
# identifier survives numeric and text; nothing survives a non-value target.
_SHAPES: Mapping[str, tuple[DomainTag, Mapping[CastTarget, DomainTag]]] = {
    "pinned_money": (
        _USD,
        {CastTarget.NUMERIC: _USD, CastTarget.TEXT: NAKED, CastTarget.OTHER: NAKED},
    ),
    "per_row_money": (
        _PER_ROW,
        {CastTarget.NUMERIC: _PER_ROW, CastTarget.TEXT: NAKED, CastTarget.OTHER: NAKED},
    ),
    "identifier": (
        _ENTITY_ID,
        {CastTarget.NUMERIC: _ENTITY_ID, CastTarget.TEXT: _ENTITY_ID, CastTarget.OTHER: NAKED},
    ),
    "money_with_nominal": (
        _MONEY_WITH_NOMINAL,
        {
            CastTarget.NUMERIC: _MONEY_WITH_NOMINAL,
            CastTarget.TEXT: NAKED,
            CastTarget.OTHER: NAKED,
        },
    ),
    "naked": (NAKED, dict.fromkeys(CastTarget, NAKED)),
}


def _cast_output(
    select_item: str, tag: DomainTag, *, dialect: str = "duckdb", other: DomainTag = NAKED
) -> DomainTag:
    facts = {
        ColumnRef(_SRC, column): (
            Fact(
                scope=ColumnRef(_SRC, column),
                value=value,
                provenance=Declared(DeclaredSource.USER_ASSERTED),
            ),
        )
        for column, value in (("amount", tag), ("other", other))
    }
    graph = build_model_graph(
        model_uid=_MODEL.unique_id,
        sql=f"SELECT {select_item} AS out FROM charges c",
        name_to_source={"charges": _SRC},
        schema={"charges": {"amount": "DECIMAL", "other": "DECIMAL", "currency": "VARCHAR"}},
        dialect=dialect,
    )
    anns = propagate(graph, domain_type_property(domain_type_grounding(facts)))
    return anns[ColumnRef(_MODEL, "out")].value


@pytest.mark.parametrize("target", list(CastTarget), ids=lambda t: t.name)
@pytest.mark.parametrize("shape", list(_SHAPES))
def test_cast_transfer_by_tag_shape_and_target_class(shape: str, target: CastTarget) -> None:
    tag, expected = _SHAPES[shape]
    assert _cast_output(f"CAST(c.amount AS {_TARGET_SQL[target]})", tag) == expected[target]


@pytest.mark.parametrize(
    ("dialect", "spelling", "numeric", "text"),
    [
        ("duckdb", "CAST(c.amount AS {t})", "BIGINT", "VARCHAR"),
        ("duckdb", "TRY_CAST(c.amount AS {t})", "BIGINT", "VARCHAR"),
        ("duckdb", "c.amount::{t}", "BIGINT", "VARCHAR"),
        ("postgres", "c.amount::{t}", "NUMERIC", "TEXT"),
        ("bigquery", "SAFE_CAST(c.amount AS {t})", "INT64", "STRING"),
        ("mysql", "CAST(c.amount AS {t})", "SIGNED", "CHAR"),
    ],
)
@pytest.mark.parametrize("keeps", [True, False], ids=["numeric", "text"])
def test_every_cast_spelling_decides_alike(
    dialect: str, spelling: str, numeric: str, text: str, keeps: bool
) -> None:
    out = _cast_output(spelling.format(t=numeric if keeps else text), _USD, dialect=dialect)
    assert out == (_USD if keeps else NAKED)


@pytest.mark.parametrize("scale", ["DECIMAL(18, 2)", "DECIMAL(10, 0)", "NUMERIC(38, 9)", "FLOAT"])
def test_narrowing_or_widening_a_numeric_keeps_the_unit(scale: str) -> None:
    assert _cast_output(f"CAST(c.amount AS {scale})", _USD) == _USD


def test_a_cast_inside_arithmetic_keeps_the_unit_through_the_expression() -> None:
    assert _cast_output("CAST(c.amount AS BIGINT) * 2", _USD) == _USD


def test_a_cast_to_text_inside_arithmetic_loses_the_unit() -> None:
    # The no-claim operand is an unknown addend, so the sum stays no-claim.
    assert _cast_output("CAST(c.amount AS VARCHAR) + 1", _USD) == NAKED


@pytest.mark.parametrize(
    ("target", "expected"),
    [("BIGINT", CONFLICT), ("VARCHAR", NAKED), ("DATE", NAKED)],
)
def test_a_conflict_rides_through_numeric_casts_only(target: str, expected: DomainTag) -> None:
    # Mixed currencies summed at this node are the finding; a numeric cast of the sum
    # keeps carrying it, a cast to a non-numeric type no longer holds a magnitude.
    eur = tagged(dimension=Dimension.of(Concrete("eur")))
    assert _cast_output(f"CAST(c.amount + c.other AS {target})", _USD, other=eur) == expected
