"""``relation_lookup_keys``: the keys a name-keyed map should index a relation under.

A real dbt-compiled reference is always schema-qualified (``ref``/``source`` render
``schema.identifier``), but hand-written SQL (tests, or a single-schema project) often
stays bare. ``relation_lookup_keys`` pairs both forms so a builder fills a map once per
node and a lookup keyed by whichever form the parsed SQL actually carries still hits.
See CodeRabbit's review of PR #270: a bare name alone let two relations sharing it
across different schemas collide onto one key.
"""

from __future__ import annotations

from dblect.manifest import relation_lookup_keys
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source


def test_bare_and_qualified_keys_are_identical_when_qualification_adds_nothing() -> None:
    n = _node("model.shop.orders", "select 1", raw="select 1", name="orders", schema=None)

    assert n.relation_name == "orders"
    assert n.qualified_relation_name == "orders"
    assert relation_lookup_keys(n) == ("orders",)


def test_schema_qualified_key_is_paired_with_the_bare_one() -> None:
    n = _node("model.shop.orders", "select 1", raw="select 1", name="orders", schema="analytics")

    assert n.relation_name == "orders"
    assert n.qualified_relation_name == "analytics.orders"
    assert relation_lookup_keys(n) == ("orders", "analytics.orders")


def test_identifier_wins_over_name_in_both_forms() -> None:
    # dbt compiles ref()/source() to the alias/identifier, not the dbt-project name;
    # both lookup keys must carry the identifier, matching Node.relation_name.
    n = _source("source.shop.orders", name="orders", identifier="orders_v2", schema="raw")

    assert relation_lookup_keys(n) == ("orders_v2", "raw.orders_v2")


def test_two_relations_sharing_a_bare_name_key_separately_once_qualified() -> None:
    a = _source("source.shop.schema_a.orders", name="orders", schema="schema_a")
    b = _source("source.shop.schema_b.orders", name="orders", schema="schema_b")

    keys_a = relation_lookup_keys(a)
    keys_b = relation_lookup_keys(b)

    assert keys_a == ("orders", "schema_a.orders")
    assert keys_b == ("orders", "schema_b.orders")
    # The bare key is shared (an unqualified reference can't itself disambiguate them),
    # but the qualified keys never collide.
    assert keys_a[1] != keys_b[1]
