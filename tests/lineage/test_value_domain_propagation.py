"""Transfer-table tests for the value-domain property.

Each transfer rule is named and closed rather than inferred by a generic fold:
an unmodelled operator must widen to ``Unbounded`` rather than silently keep
the child's set, and a ``NULL`` literal must ground the empty set rather than
top (or every ELSE-less ``CASE`` and every ``COALESCE(x, NULL)`` would go
silent). Rows are ordered with those two first, as the ones most likely to be
gotten wrong.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from dblect.lineage import propagate
from dblect.lineage.builder import build_model_graph
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.predicate import Lit, LitKind
from dblect.lineage.properties.value_domain import (
    UNBOUNDED,
    Bounded,
    ValueDomain,
    value_domain_property,
)
from tests.lineage._propagation_table import PropagationCase, run_propagation_case

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.orders")
_MODEL = SourceRef(SourceKind.MODEL, "model.shop.m")
_SCHEMA = {"orders": {"status": "VARCHAR", "status2": "VARCHAR", "k": "INT"}}


def _str_set(*values: str) -> Bounded:
    return Bounded(frozenset(Lit(LitKind.STR, v) for v in values))


def _facts(
    by_column: Mapping[str, ValueDomain],
) -> Mapping[ColumnRef, tuple[Fact[ValueDomain, ColumnRef], ...]]:
    out: dict[ColumnRef, tuple[Fact[ValueDomain, ColumnRef], ...]] = {}
    for column, value in by_column.items():
        ref = ColumnRef(_SRC, column)
        out[ref] = (
            Fact(scope=ref, value=value, provenance=Declared(DeclaredSource.USER_ASSERTED)),
        )
    return out


def _run(sql: str, facts: Mapping[str, ValueDomain]) -> Mapping[str, ValueDomain]:
    graph = build_model_graph(
        model_uid=_MODEL.unique_id, sql=sql, name_to_source={"orders": _SRC}, schema=_SCHEMA
    )
    prop = value_domain_property(_facts(facts))
    anns = propagate(graph, prop)
    return {ref.column: ann.value for ref, ann in anns.items() if ref.source == _MODEL}


_CASES: tuple[PropagationCase[ValueDomain], ...] = (
    PropagationCase(
        "upper_widens_to_unbounded",
        # The catch-all row: a fold that let a single-child node pass its
        # value through untouched would leave this narrow instead.
        "SELECT upper(o.status) AS r FROM orders o",
        "r",
        UNBOUNDED,
        {"status": _str_set("shipped", "pending")},
    ),
    PropagationCase(
        "null_literal_grounds_the_empty_set",
        "SELECT NULL AS r FROM orders o",
        "r",
        Bounded(frozenset()),
        {},
    ),
    PropagationCase(
        "string_literal_is_a_singleton",
        "SELECT 'shipped' AS r FROM orders o",
        "r",
        _str_set("shipped"),
        {},
    ),
    PropagationCase(
        "rename_preserves_the_set",
        "SELECT o.status AS r FROM orders o",
        "r",
        _str_set("shipped"),
        {"status": _str_set("shipped")},
    ),
    PropagationCase(
        "searched_case_with_no_else_keeps_exactly_its_then_literals",
        "SELECT CASE WHEN o.status = 'shipped' THEN 'S' WHEN o.status = 'pending' THEN 'P' "
        "END AS r FROM orders o",
        "r",
        _str_set("S", "P"),
        {"status": _str_set("shipped", "pending")},
    ),
    PropagationCase(
        "simple_case_remap_keeps_only_then_and_else_never_when",
        # The tested column's own domain must not leak into the result: only
        # the THEN/ELSE literals the CASE actually produces belong in its set.
        "SELECT CASE o.status WHEN 'shipped' THEN 'S' WHEN 'pending' THEN 'P' ELSE 'O' END AS r "
        "FROM orders o",
        "r",
        _str_set("S", "P", "O"),
        {"status": _str_set("shipped", "pending", "cancelled")},
    ),
    PropagationCase(
        "coalesce_unions_its_children",
        "SELECT COALESCE(o.status, 'unknown') AS r FROM orders o",
        "r",
        _str_set("shipped", "pending", "unknown"),
        {"status": _str_set("shipped", "pending")},
    ),
    PropagationCase(
        "coalesce_with_a_null_literal_keeps_the_set",
        # If NULL grounded top instead of the empty set, this would wrongly
        # widen to Unbounded.
        "SELECT COALESCE(o.status, NULL) AS r FROM orders o",
        "r",
        _str_set("shipped"),
        {"status": _str_set("shipped")},
    ),
    PropagationCase(
        "aggregate_widens_to_unbounded",
        "SELECT MAX(o.status) AS r FROM orders o",
        "r",
        UNBOUNDED,
        {"status": _str_set("shipped")},
    ),
    PropagationCase(
        "union_of_two_enumd_columns_unions_their_sets",
        "SELECT o.status AS r FROM orders o UNION ALL SELECT o.status2 AS r FROM orders o",
        "r",
        _str_set("shipped", "pending"),
        {"status": _str_set("shipped"), "status2": _str_set("pending")},
    ),
    PropagationCase(
        "union_of_a_bounded_and_an_unbounded_side_is_unbounded",
        "SELECT o.status AS r FROM orders o UNION ALL SELECT upper(o.status2) AS r FROM orders o",
        "r",
        UNBOUNDED,
        {"status": _str_set("shipped"), "status2": _str_set("pending")},
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
def test_value_domain_propagation(case: PropagationCase[ValueDomain]) -> None:
    run_propagation_case(case, _run)


def test_a_column_that_is_only_ever_null_grounds_the_empty_set() -> None:
    """An inferred ``NULL``'s empty set must survive a passthrough rename, not
    be mistaken for "nothing declared" the way a naive falsy check on the
    empty frozenset would."""
    ann_map = _run("SELECT r AS out FROM (SELECT NULL AS r FROM orders o) sub", {})
    assert ann_map["out"] == Bounded(frozenset())
