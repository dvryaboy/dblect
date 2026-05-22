"""Parse SQL into a sqlglot AST.

The input to the analysis layer is dbt-compiled SQL (rendered by ``dbt
compile``): plain SQL that sqlglot can parse directly. `parse_sql` is a thin
wrapper around `sqlglot.parse_one` that translates parser errors into our
own `SQLParseError` so callers can distinguish "this model's SQL is broken"
from arbitrary sqlglot exceptions, and so the walker can record the
offending SQL on its skipped-model report.
"""

from __future__ import annotations

import sqlglot
from sqlglot import Expr
from sqlglot.errors import ParseError


class SQLParseError(ValueError):
    """Raised when sqlglot can't parse the input SQL."""

    def __init__(self, message: str, sql: str) -> None:
        super().__init__(message)
        self.sql = sql


def parse_sql(sql: str, dialect: str | None = None) -> Expr:
    """Parse `sql` with sqlglot under `dialect`, raising `SQLParseError` on failure.

    `dialect` is passed through to sqlglot. ``None`` selects sqlglot's
    permissive default; pass ``"duckdb"``, ``"snowflake"``, etc. when the
    SQL is dialect-specific.
    """
    try:
        return sqlglot.parse_one(sql, dialect=dialect)
    except ParseError as e:
        raise SQLParseError(str(e), sql=sql) from e
