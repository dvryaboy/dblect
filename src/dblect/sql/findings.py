"""The project-wide finding type system for SQL static analysis.

A ``Finding`` is a single structural observation about one SQL statement, located
by a line span. Every detector layer emits these: the structural pattern detectors
in :mod:`dblect.sql.patterns`, the lineage-grounded detectors under
:mod:`dblect.nullability`, :mod:`dblect.uniqueness`, :mod:`dblect.snapshot`, and
:mod:`dblect.flatten`. ``FindingKind`` is their shared vocabulary, so it lives here
rather than in any one detector module.

These are span-in-one-statement findings, distinct from the declaration-level
findings :mod:`dblect.check.findings` carries (``CheckFindingKind`` /
``CheckFinding``). The two families share the ``-- noqa`` suppression syntax, which
is why ``suppression_code`` / ``suppression_hint`` accept either kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlglot import Expr

from dblect.sql import _sqlglot as sg

if TYPE_CHECKING:
    # Referenced only in the suppression helpers' signatures; imported under
    # ``TYPE_CHECKING`` so this SQL-layer module stays free of a declaration-check
    # import at load time.
    from dblect.check.findings import CheckFindingKind


class FindingKind(StrEnum):
    NULL_GROUP_AFTER_OUTER_JOIN = "null_group_after_outer_join"
    COALESCE_ON_JOIN_KEY = "coalesce_on_join_key"
    UNORDERED_RANKING_WINDOW = "unordered_ranking_window"
    UNORDERED_AGGREGATE = "unordered_aggregate"
    WHERE_ON_OUTER_JOINED_NULLABLE = "where_on_outer_joined_nullable"
    NON_DETERMINISTIC_FUNCTION = "non_deterministic_function"
    NON_UNIQUE_WINDOW_ORDER_KEYS = "non_unique_window_order_keys"
    JOIN_FANOUT = "join_fanout"
    CROSS_MODEL_FANOUT = "cross_model_fanout"
    NULL_GROUP_ON_NULLABLE_KEY = "null_group_on_nullable_key"
    JOIN_ON_NULLABLE_KEY = "join_on_nullable_key"
    NOT_IN_NULLABLE_SUBQUERY = "not_in_nullable_subquery"
    INNER_FLATTEN_ROW_DROP = "inner_flatten_row_drop"
    SNAPSHOT_TEMPORAL_FILTER_MISSING = "snapshot_temporal_filter_missing"


def suppression_code(kind: FindingKind | CheckFindingKind) -> str:
    """The SQLFluff-style noqa code for a finding kind: ``DBLECT_`` plus the kind's
    value uppercased (e.g. ``DBLECT_JOIN_FANOUT``). The ``DBLECT_`` prefix is what
    distinguishes our codes from real lint rule codes (``RF01`` and friends), so dbt
    lint's noqa directives and ours coexist in one comment without colliding."""
    return f"DBLECT_{kind.value.upper()}"


def suppression_hint(kind: FindingKind | CheckFindingKind) -> str:
    # The suggested directive must stay valid suppression syntax (round-trip tested).
    # Both finding families share the directive, so the hint takes either kind and
    # renders the noqa code the scanner reads back.
    return f"If this is intentional, suppress it with `-- noqa: {suppression_code(kind)}`."


@dataclass(frozen=True, slots=True)
class Finding:
    """A single static-analysis observation about a SQL statement.

    ``line_start`` and ``line_end`` are 1-indexed line numbers in the SQL
    the detector was given — the model's ``compiled_code``, which dbt
    renders with refs and macro calls expanded inline. Line numbers
    correspond to the compiled output; the reporter still surfaces the
    model's source file path so navigation works as expected.

    A value of ``0`` means we couldn't pin the finding to a line, which
    happens when the offending AST node has no ``Identifier`` descendants
    sqlglot stamped with position info. Callers can treat ``0`` as "model
    scope, line unknown" and report it without a line number.
    """

    kind: FindingKind
    message: str
    sql_snippet: str
    line_start: int
    line_end: int


def finding_at(kind: FindingKind, *, message: str, node: Expr) -> Finding:
    """Build a Finding whose snippet and source span both come from `node`."""
    span = sg.line_range(node)
    line_start, line_end = span if span is not None else (0, 0)
    return Finding(
        kind=kind,
        message=message,
        sql_snippet=sg.render_sql(node),
        line_start=line_start,
        line_end=line_end,
    )
