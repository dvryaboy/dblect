"""Parse SQL (with dbt Jinja) into a sqlglot AST plus a record of redacted Jinja.

The static-analysis layer walks sqlglot's AST directly (see decision 16 in
``questions_and_decisions.md``). Before sqlglot sees the SQL, we redact dbt
Jinja so the parser doesn't choke on tags. Two flavours of placeholder remain
in the parsed tree:

* ``ref('x')`` becomes a bare identifier ``x`` so the structural patterns
  (joins, group-bys, lineage between CTEs) read naturally.
* Every other ``{{ expr }}`` becomes a unique sentinel identifier
  (``__jinja_001`` and so on). ``{% ... %}`` statement tags and ``{# ... #}``
  comments are stripped entirely; the body of a ``{% for %}`` block stays in
  place, exercised once.

A `JinjaPlaceholder` captures the original text and the substitution, so a
detector that flags a sentinel can recover the source span.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from itertools import count
from typing import Self

import sqlglot
from sqlglot import Expr
from sqlglot.errors import ParseError


class PlaceholderKind(StrEnum):
    REF = "ref"
    SOURCE = "source"
    EXPR = "expr"


@dataclass(frozen=True, slots=True)
class JinjaPlaceholder:
    """One Jinja construct that was redacted before parsing.

    `sentinel` is the identifier (or literal) that took the construct's place
    in the redacted SQL the parser saw; `original` is the verbatim Jinja
    fragment from the source. For ``ref('x')`` and ``source('s', 't')``,
    `target` carries the referenced name.
    """

    sentinel: str
    original: str
    kind: PlaceholderKind
    target: str | None


class SQLParseError(ValueError):
    """Raised when sqlglot can't parse the (post-Jinja-redaction) SQL."""

    def __init__(self, message: str, redacted_sql: str) -> None:
        super().__init__(message)
        self.redacted_sql = redacted_sql


@dataclass(frozen=True, slots=True)
class ParsedSQL:
    """A parsed SQL statement plus the redaction record.

    `tree` is sqlglot's expression tree. Detectors in ``dblect.sql.patterns``
    walk it directly. `placeholders` is the in-order list of Jinja constructs
    that were rewritten before parsing; sentinels in the AST resolve back to
    placeholders by `sentinel` equality.
    """

    raw: str
    redacted: str
    dialect: str | None
    tree: Expr
    placeholders: tuple[JinjaPlaceholder, ...]

    @classmethod
    def parse(cls, sql: str, dialect: str | None = None) -> Self:
        """Redact dbt Jinja in `sql`, parse with sqlglot, return a `ParsedSQL`.

        `dialect` is passed through to sqlglot. ``None`` selects sqlglot's
        permissive default; pass ``"duckdb"``, ``"snowflake"``, etc. when the
        SQL is dialect-specific.
        """
        redacted, placeholders = _redact_jinja(sql)
        try:
            tree = sqlglot.parse_one(redacted, dialect=dialect)
        except ParseError as e:
            raise SQLParseError(str(e), redacted_sql=redacted) from e
        return cls(
            raw=sql,
            redacted=redacted,
            dialect=dialect,
            tree=tree,
            placeholders=tuple(placeholders),
        )

    @property
    def refs(self) -> tuple[str, ...]:
        """Names of dbt models referenced via ``{{ ref('...') }}`` in order."""
        return tuple(
            p.target
            for p in self.placeholders
            if p.kind is PlaceholderKind.REF and p.target is not None
        )


_JINJA_COMMENT = re.compile(r"{#.*?#}", re.DOTALL)
_JINJA_STATEMENT = re.compile(r"{%-?.*?-?%}", re.DOTALL)
_JINJA_EXPR = re.compile(r"{{-?(.*?)-?}}", re.DOTALL)
_REF_CALL = re.compile(r"^ref\(\s*['\"]([^'\"]+)['\"]\s*\)$")
_SOURCE_CALL = re.compile(r"^source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)$")


def _redact_jinja(sql: str) -> tuple[str, list[JinjaPlaceholder]]:
    out = _JINJA_COMMENT.sub("", sql)
    out = _JINJA_STATEMENT.sub("", out)

    placeholders: list[JinjaPlaceholder] = []
    counter = count(1)

    def sub_expr(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        ref_m = _REF_CALL.match(body)
        if ref_m is not None:
            target = ref_m.group(1)
            placeholders.append(
                JinjaPlaceholder(
                    sentinel=target,
                    original=match.group(0),
                    kind=PlaceholderKind.REF,
                    target=target,
                )
            )
            return target
        src_m = _SOURCE_CALL.match(body)
        if src_m is not None:
            source_name, table_name = src_m.group(1), src_m.group(2)
            sentinel = f"{source_name}__{table_name}"
            placeholders.append(
                JinjaPlaceholder(
                    sentinel=sentinel,
                    original=match.group(0),
                    kind=PlaceholderKind.SOURCE,
                    target=f"{source_name}.{table_name}",
                )
            )
            return sentinel
        sentinel = f"__jinja_{next(counter):03d}"
        placeholders.append(
            JinjaPlaceholder(
                sentinel=sentinel,
                original=match.group(0),
                kind=PlaceholderKind.EXPR,
                target=None,
            )
        )
        return sentinel

    out = _JINJA_EXPR.sub(sub_expr, out)
    return out, placeholders


def is_jinja_sentinel(name: str, placeholders: Iterable[JinjaPlaceholder]) -> bool:
    """True if `name` matches a sentinel emitted by Jinja redaction."""
    return any(p.sentinel == name for p in placeholders)
