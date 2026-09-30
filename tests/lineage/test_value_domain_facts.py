"""The ``accepted_values`` discoverer: manifest-backed value-domain facts.

Mirrors ``test_nullability_facts.py``'s pattern (dblect-shaped manifests built
directly) and, for the ``where``-scoped case, its exact decision: a ``where``
filter does not drop the fact, but grounding does not fold a conditional claim
into the unconditional annotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from dblect.lineage.facts.model import Declared, DeclaredSource, Predicate
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.predicate import Lit, LitKind
from dblect.lineage.properties.value_domain import Bounded, accepted_values_discoverer
from dblect.manifest import DbtTestMetadata, Node, ResourceType
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source


def _accepted_values_test(
    uid: str,
    *,
    column: str | None,
    target: str,
    values: object,
    where: str | None = None,
    enabled: bool = True,
) -> Node:
    kwargs: dict[str, object] = {"values": values}
    if column is not None:
        kwargs["column_name"] = column
    return _node(
        uid,
        kind=ResourceType.OTHER,
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(
            name="accepted_values", kwargs=kwargs, enabled=enabled, where=where
        ),
        attached_node=target,
    )


def _str_set(*values: str) -> Bounded:
    return Bounded(frozenset(Lit(LitKind.STR, v) for v in values))


def test_accepted_values_test_grounds_a_bounded_string_set() -> None:
    src = _source("source.shop.raw.orders")
    test = _accepted_values_test(
        "test.shop.av", column="status", target=src.unique_id, values=["shipped", "pending"]
    )
    facts = list(accepted_values_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert len(facts) == 1
    fact = facts[0]
    assert fact.scope == ColumnRef(SourceRef(SourceKind.SOURCE, src.unique_id), "status")
    assert fact.value == _str_set("shipped", "pending")
    assert fact.provenance == Declared(DeclaredSource.DBT_GENERIC_TEST)


def test_accepted_values_test_on_model_column() -> None:
    model = _node("model.shop.fct_orders", "select 1")
    test = _accepted_values_test(
        "test.shop.av", column="status", target=model.unique_id, values=["shipped"]
    )
    facts = list(accepted_values_discoverer().discover(_manifest(model, test), name_to_source={}))
    assert facts[0].scope == ColumnRef(SourceRef(SourceKind.MODEL, model.unique_id), "status")


def test_accepted_values_column_name_is_case_folded() -> None:
    src = _source("source.shop.raw.orders")
    test = _accepted_values_test(
        "test.shop.av", column="Status", target=src.unique_id, values=["shipped"]
    )
    facts = list(accepted_values_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert facts[0].scope.column == "status"


def test_accepted_values_numeric_values() -> None:
    src = _source("source.shop.raw.orders")
    test = _accepted_values_test(
        "test.shop.av", column="priority", target=src.unique_id, values=[1, 2, 3]
    )
    facts = list(accepted_values_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert facts[0].value == Bounded(frozenset({Lit(LitKind.NUM, Decimal(n)) for n in (1, 2, 3)}))


def test_conditional_accepted_values_test_is_captured_with_its_predicate() -> None:
    src = _source("source.shop.raw.orders")
    test = _accepted_values_test(
        "test.shop.av",
        column="status",
        target=src.unique_id,
        values=["shipped"],
        where="country = 'US'",
    )
    facts = list(accepted_values_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert len(facts) == 1
    assert facts[0].value == _str_set("shipped")
    assert facts[0].condition == Predicate("country = 'US'")


def test_non_accepted_values_test_is_ignored() -> None:
    src = _source("source.shop.raw.orders")
    other = _node(
        "test.shop.nn",
        kind=ResourceType.OTHER,
        depends_on=frozenset({src.unique_id}),
        test_metadata=DbtTestMetadata(name="not_null", kwargs={"column_name": "status"}),
        attached_node=src.unique_id,
    )
    facts = list(accepted_values_discoverer().discover(_manifest(src, other), name_to_source={}))
    assert facts == []


# --- shapes that ground nothing rather than a set silently missing a member --
#
# Each of these is a closed reason the discoverer's own docstring gives for
# skipping a test: dropping just the offending member would understate the
# declared set and could make a real value look dead, so the whole test's
# fact is dropped instead.


@dataclass(frozen=True, slots=True)
class _NothingGroundedCase:
    id: str
    column: str | None
    values: object
    enabled: bool = True


_NOTHING_GROUNDED_CASES: tuple[_NothingGroundedCase, ...] = (
    _NothingGroundedCase("disabled_test", "status", ["shipped"], enabled=False),
    _NothingGroundedCase("missing_column_name", None, ["shipped"]),
    _NothingGroundedCase("boolean_member", "flag", ["a", True]),
    _NothingGroundedCase("null_member", "status", ["a", None]),
    _NothingGroundedCase("nested_list_member", "status", ["a", ["b"]]),
    _NothingGroundedCase("empty_values_list", "status", []),
    _NothingGroundedCase("non_list_values_kwarg", "status", "shipped"),
)


@pytest.mark.parametrize("case", _NOTHING_GROUNDED_CASES, ids=lambda c: c.id)
def test_a_malformed_test_grounds_nothing(case: _NothingGroundedCase) -> None:
    src = _source("source.shop.raw.orders")
    test = _accepted_values_test(
        "test.shop.av",
        target=src.unique_id,
        column=case.column,
        values=case.values,
        enabled=case.enabled,
    )
    facts = list(accepted_values_discoverer().discover(_manifest(src, test), name_to_source={}))
    assert facts == []
