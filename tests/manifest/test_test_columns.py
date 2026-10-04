"""Column names in dbt test kwargs are unrendered Jinja. A name the manifest does
not state literally is unusable: it must never reach a fact as a phantom column,
and a key built from a partly unusable list is not a key."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

import pytest

from dblect.lineage.facts.model import Fact
from dblect.lineage.facts.property import FactDiscoverer
from dblect.lineage.properties.nullability import not_null_test_discoverer
from dblect.lineage.properties.uniqueness import (
    CandidateKeySet,
    unique_combination_discoverer,
    unique_test_discoverer,
)
from dblect.lineage.properties.value_domain import accepted_values_discoverer
from dblect.manifest import DbtTestMetadata, Node, ResourceType
from dblect.manifest.parse import declared_column_name
from dblect.types.bridge import dbt_relationship_edges
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_SHAPES: tuple[tuple[object, str | None], ...] = (
    ("plan", "plan"),
    ("{{ quote_column('plan') }}", "plan"),
    ('{{ quote_column("plan") }}', "plan"),
    ("{{quote_column('plan')}}", "plan"),
    ("{{ adapter.quote('plan') }}", "plan"),
    ('{{ adapter.quote("plan") }}', "plan"),
    ("{{ var('c') }}", None),
    ("{{ quote_column(var('c')) }}", None),
    ("{{ quote_column('a') }}_x", None),
    ("{{ quote_column('a') }}{{ quote_column('b') }}", None),
    ("{% if x %}a{% endif %}", None),
    ("{# note #}plan", None),
    ("a {{ x }}", None),
    ("", None),
    (None, None),
    (3, None),
    (["plan"], None),
)


@pytest.mark.parametrize(("raw", "expected"), _SHAPES)
def test_declared_column_name_over_every_entry_shape(raw: object, expected: str | None) -> None:
    assert declared_column_name(raw) == expected


_JINJA = "{{ var('c') }}"
_QUOTED = "{{ quote_column('plan') }}"
_TARGET = "model.shop.m"
_PARENT = "model.shop.p"
_COMBINATION = "dbt_utils.unique_combination_of_columns"


def _tm_node(name: str, kwargs: Mapping[str, object], *deps: str) -> Node:
    return _node(
        "test.shop.t",
        kind=ResourceType.OTHER,
        depends_on=frozenset({_TARGET, *deps}),
        test_metadata=DbtTestMetadata(name=name, kwargs=dict(kwargs)),
        attached_node=_TARGET,
    )


def _facts(discoverer: FactDiscoverer[Any, Any], test: Node) -> Collection[Fact[Any, Any]]:
    m = _manifest(_node(_TARGET, "select 1"), _node(_PARENT, "select 1"), test)
    return discoverer.discover(m, name_to_source={})


def test_unique_on_jinja_column_grounds_nothing() -> None:
    assert not _facts(unique_test_discoverer(), _tm_node("unique", {"column_name": _JINJA}))


def test_unique_on_quote_idiom_grounds_the_named_column() -> None:
    (fact,) = _facts(unique_test_discoverer(), _tm_node("unique", {"column_name": _QUOTED}))
    assert fact.value == CandidateKeySet.of(frozenset({"plan"}))


def test_not_null_on_jinja_column_grounds_nothing() -> None:
    assert not _facts(not_null_test_discoverer(), _tm_node("not_null", {"column_name": _JINJA}))


def test_not_null_on_quote_idiom_grounds_the_named_column() -> None:
    (fact,) = _facts(not_null_test_discoverer(), _tm_node("not_null", {"column_name": _QUOTED}))
    assert fact.scope.column == "plan"


def test_accepted_values_on_jinja_column_grounds_nothing() -> None:
    test = _tm_node("accepted_values", {"column_name": _JINJA, "values": ["a"]})
    assert not _facts(accepted_values_discoverer(), test)


@pytest.mark.parametrize("column_kwarg", ["column_name", "field"])
def test_relationships_with_a_jinja_side_yields_no_edge(column_kwarg: str) -> None:
    kwargs = {"column_name": "c", "field": "f", "to": "ref('p')"} | {column_kwarg: _JINJA}
    m = _manifest(
        _node(_TARGET, "select 1"),
        _node(_PARENT, "select 1"),
        _tm_node("relationships", kwargs, _PARENT),
    )
    assert dbt_relationship_edges(m) == ()


@pytest.mark.parametrize(
    "columns",
    [[_JINJA], ["a", _JINJA], [_JINJA, "a", "b"], ["a", _QUOTED, _JINJA]],
)
def test_combination_with_any_jinja_entry_grounds_no_key(columns: list[str]) -> None:
    test = _tm_node(_COMBINATION, {"combination_of_columns": columns})
    assert not _facts(unique_combination_discoverer(), test)


def test_combination_with_quote_idiom_grounds_the_named_columns() -> None:
    test = _tm_node(_COMBINATION, {"combination_of_columns": ["a", _QUOTED]})
    (fact,) = _facts(unique_combination_discoverer(), test)
    assert fact.value == CandidateKeySet.of(frozenset({"a", "plan"}))
