"""``copied_column``: what counts as an unchanged copy of one column."""

from __future__ import annotations

import pytest
import sqlglot
import sqlglot.expressions as exp

from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.property import attach_column_ref, copied_column

_ORIGIN = ColumnRef(SourceRef(SourceKind.MODEL, "model.shop.stg"), "currency")


def _derivation(sql: str, dialect: str):
    expr = sqlglot.parse_one(sql, read=dialect)
    for col in expr.find_all(exp.Column):
        attach_column_ref(col, _ORIGIN)
    return expr


@pytest.mark.parametrize(
    ("sql", "dialect", "whole"),
    [
        ("CAST(currency AS TEXT)", "postgres", True),
        ("CAST(currency AS VARCHAR)", "postgres", True),
        ("CAST(currency AS VARCHAR(1))", "postgres", False),
        ("CAST(currency AS CHAR(3))", "postgres", False),
        ("CAST(currency AS INT)", "postgres", False),
        ("CAST(currency AS DATE)", "postgres", False),
        ("(currency) AS code", "postgres", True),
    ],
)
def test_whole_value_admits_only_an_unsized_text_cast(sql: str, dialect: str, whole: bool) -> None:
    derivation = _derivation(sql, dialect)
    assert (copied_column(derivation, whole_value=True) == _ORIGIN) is whole
    # Without the flag, any cast is a function of the one column.
    assert copied_column(derivation) == _ORIGIN
