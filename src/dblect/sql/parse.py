"""Parse dbt-compiled SQL into a sqlglot AST, reducing a compiled script to its result.

A model usually compiles to a single ``SELECT``, but a macro that emits a helper UDF
inline produces a script: leading ``CREATE [TEMPORARY] FUNCTION`` / ``DECLARE`` /
``SET`` statements before the terminal query. `parse_result_statement` keeps the final
top-level query and drops the prelude. A call to an inline-defined function stays an
ordinary call, which the propagator reads as a value-erasing transform, so an inline
UDF degrades to the opaque posture rather than failing the parse.

A script with more than one query, or none, cannot be reduced to a single model, so
the caller records a resolution-coverage miss instead of guessing. `parse_sql` is the
single-statement door the detectors call; it sees through a prelude and raises
`SQLParseError` when the SQL is unparseable or has no single result statement.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import Expr
from sqlglot import expressions as exp
from sqlglot.errors import ParseError


class SQLParseError(ValueError):
    """Raised when sqlglot can't parse the input SQL."""

    def __init__(self, message: str, sql: str) -> None:
        super().__init__(message)
        self.sql = sql


@dataclass(frozen=True, slots=True)
class SingleResult:
    """The script reduced to one result statement: the query the analysis walks."""

    statement: Expr


@dataclass(frozen=True, slots=True)
class MultiResultScript:
    """More than one result-producing statement (genuine scripting): a coverage miss,
    since picking the model statement would be a guess."""

    result_count: int


@dataclass(frozen=True, slots=True)
class NoResultScript:
    """No query to follow lineage on (an ``INSERT ... VALUES`` or DDL-only body): a
    coverage miss."""


# The outcome of reducing a compiled script to its result statement.
ResultStatement = SingleResult | MultiResultScript | NoResultScript


def _result_query(statement: Expr) -> Expr | None:
    """The result-producing query ``statement`` carries, or ``None``.

    A bare ``exp.Query`` (``SELECT``, set operation, CTE chain) is its own result. A
    materialization wrapper (``CREATE TABLE/VIEW ... AS SELECT``, ``INSERT ... SELECT``)
    holds the model's logic in ``expression``, so the detectors analyse that inner query
    rather than dropping the model. ``MERGE`` keeps its source in a ``using`` clause with
    no single result query, so it is deliberately left as a non-result.
    """
    if isinstance(statement, exp.Query):
        return statement
    if isinstance(statement, exp.Create | exp.Insert):
        inner = statement.expression
        if isinstance(inner, exp.Query):
            return inner
    return None


def parse_result_statement(sql: str, dialect: str | None = None) -> ResultStatement:
    """Parse ``sql`` as a (possibly multi-statement) compiled script and reduce it to
    its result statement.

    Returns :class:`SingleResult` when exactly one statement is result-producing,
    :class:`MultiResultScript` when more than one is (genuine scripting), and
    :class:`NoResultScript` when none is. Raises :class:`SQLParseError` when sqlglot
    cannot parse the input at all.
    """
    try:
        statements = sqlglot.parse(sql, dialect=dialect)
    except ParseError as e:
        raise SQLParseError(str(e), sql=sql) from e
    results = [q for s in statements if s is not None if (q := _result_query(s)) is not None]
    if len(results) == 1:
        return SingleResult(statement=results[0])
    if len(results) > 1:
        return MultiResultScript(result_count=len(results))
    return NoResultScript()


def parse_sql(sql: str, dialect: str | None = None) -> Expr:
    """Parse ``sql`` and return its single result statement, seeing through a DDL
    prelude. Raises `SQLParseError` when the SQL is unparseable or cannot be reduced to
    one result statement; callers that want the structured outcome (to record a
    coverage miss rather than a skip) use :func:`parse_result_statement` directly.

    `dialect` is passed through to sqlglot; ``None`` selects its permissive default.
    """
    outcome = parse_result_statement(sql, dialect=dialect)
    match outcome:
        case SingleResult(statement=statement):
            return statement
        case MultiResultScript(result_count=count):
            raise SQLParseError(
                f"compiled SQL has {count} result-producing statements; "
                "cannot reduce to a single model statement",
                sql=sql,
            )
        case NoResultScript():
            raise SQLParseError(
                "compiled SQL has no result-producing statement to analyse",
                sql=sql,
            )
