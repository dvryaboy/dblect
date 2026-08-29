"""Relation-scoped functional-dependency propagation, end to end through the substrate.

These pin the FD relation walk at the contract boundary: build a manifest of
sources and models, ground declared dependencies from synthetic facts (the typed
contract bridge is a later build), run the one propagator, and read each
relation's FD set. The rules under test are the sound ones the walk can justify:
a passthrough carries the source's dependencies, a projection renames them and
drops what it cannot carry, a WHERE preserves them and pins filtered columns
constant, a GROUP BY determines every other output from the group key, a key read
from the uniqueness property determines the columns selected alongside it, a join
carries its kept sides' dependencies plus an inner join's ON equalities, and a
UNION keeps exactly the declared dependencies every arm carries from one
declaration while everything derived drops.

The union rule rests on the pair-coverage argument: a dependency is universally
quantified over row pairs, and a union adds exactly the cross pairs, one row from
each arm. A derived dependency's witness is arm-local (each arm can pin a column
to a different constant), so the cross pairs are uncovered and it dies. A declared
dependency is an axiom about the declaring relation's world, so when every arm's
pairs are that world's pairs, the cross pairs are covered too and the merge keeps
it, with the same grounding.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_relation_graph
from dblect.lineage.facts.model import (
    BASE_WORLD,
    Annotation,
    CompileOrigin,
    CompileValue,
    Declared,
    DeclaredSource,
    Fact,
    NativeConstraint,
    Provenance,
)
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.functional_dependency import (
    FD,
    NO_FDS,
    DeclaredFD,
    FDSet,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.properties.uniqueness import uniqueness_property
from dblect.lineage.property import propagate
from dblect.manifest import DbtTestMetadata, Node, ResourceType
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source

_DUCKDB = profile_for_adapter("duckdb")

_FdFacts = Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]]


def _unique(uid: str, *, column: str, target: str) -> Node:
    return _node(
        uid,
        kind=ResourceType.OTHER,
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}),
        attached_node=target,
    )


_PAYMENTS = SourceRef(SourceKind.SOURCE, "source.shop.raw.payments")


def _declared(*fds: FD, scope: SourceRef = _PAYMENTS) -> _FdFacts:
    fact = Fact(
        scope=scope, value=FDSet.of(*fds), provenance=Declared(DeclaredSource.USER_ASSERTED)
    )
    return {scope: (fact,)}


def _fd(dependent: str, *determinant: str) -> FD:
    return FD(frozenset(determinant), dependent)


def _inst(fd: FD, *, origin: SourceRef = _PAYMENTS, as_fd: FD | None = None) -> DeclaredFD:
    """A declared dependency's instance: ``fd`` as declared at ``origin``, carried
    under the current relation's names (``as_fd`` when a projection renamed it)."""
    return DeclaredFD(origin=origin, declared=fd, fd=fd if as_fd is None else as_fd)


def _carried(*instances: DeclaredFD, derived: tuple[FD, ...] = ()) -> FDSet:
    """The value a relation holds when it carries these declared instances plus
    any purely derived dependencies."""
    return FDSet(frozenset(i.fd for i in instances) | frozenset(derived), frozenset(instances))


def _fds(facts: _FdFacts, *nodes: Node, read_keys: bool = False) -> dict[str, FDSet]:
    """Build a manifest from the nodes, propagate the FD property (after uniqueness
    when ``read_keys`` is set, so the key-derived source is live), and return each
    model's FD set keyed by unique_id."""
    manifest = _manifest(*nodes)
    graph = build_relation_graph(manifest).graph
    ground = functional_dependency_grounding(facts)
    if read_keys:
        uniq = uniqueness_property(manifest, _DUCKDB)
        store = AnnotationStore()
        for scope, ann in propagate(graph, uniq).items():
            store.record(uniq.name, scope, ann)
        prop = functional_dependency_property(ground, uniqueness=uniq.ref)
        ctx = PropertyRegistry((uniq, prop)).dep_context(store)
        anns: Mapping[SourceRef, Annotation[FDSet]] = propagate(graph, prop, dep_context=ctx)
    else:
        anns = propagate(graph, functional_dependency_property(ground))
    return {ref.unique_id: ann.value for ref, ann in anns.items() if ref.kind is SourceKind.MODEL}


# --- carrying and renaming -----------------------------------------------------


def test_passthrough_carries_the_declared_fd() -> None:
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _node("model.shop.stg", "SELECT country, currency, amount FROM payments"),
    )
    assert out["model.shop.stg"] == _carried(_inst(_fd("currency", "country")))


def test_projection_renames_both_sides() -> None:
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _node("model.shop.stg", "SELECT country AS nation, currency AS curr FROM payments"),
    )
    assert out["model.shop.stg"] == _carried(
        _inst(_fd("currency", "country"), as_fd=_fd("curr", "nation"))
    )


def test_dropping_a_dependency_column_drops_the_fd() -> None:
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _node("model.shop.stg", "SELECT country, amount FROM payments"),
    )
    assert out["model.shop.stg"] == NO_FDS


def test_star_carries_everything() -> None:
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _node("model.shop.stg", "SELECT * FROM payments"),
    )
    assert out["model.shop.stg"] == _carried(_inst(_fd("currency", "country")))


# --- WHERE ----------------------------------------------------------------------


def test_where_preserves_fds() -> None:
    """A filter removes rows, and a dependency that holds on all rows holds on any
    subset, so the FD survives."""
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _node("model.shop.stg", "SELECT country, currency FROM payments WHERE amount > 0"),
    )
    assert out["model.shop.stg"] == _carried(_inst(_fd("currency", "country")))


def test_where_equality_pins_a_column_constant() -> None:
    """``WHERE currency = 'usd'`` makes ``currency`` single-valued over the result,
    which is the empty-determinant dependency."""
    out = _fds(
        _declared(),
        _source(_PAYMENTS.unique_id),
        _node("model.shop.usd", "SELECT country, currency FROM payments WHERE currency = 'usd'"),
    )
    assert out["model.shop.usd"] == FDSet.of(_fd("currency"))


def test_constancy_flows_through_a_cte_and_a_downstream_model() -> None:
    out = _fds(
        _declared(),
        _source(_PAYMENTS.unique_id),
        _node(
            "model.shop.usd",
            "WITH f AS (SELECT country, currency FROM payments WHERE currency = 'usd') "
            "SELECT country, currency FROM f",
        ),
        _node("model.shop.downstream", "SELECT country, currency FROM usd"),
    )
    assert out["model.shop.usd"] == FDSet.of(_fd("currency"))
    assert out["model.shop.downstream"] == FDSet.of(_fd("currency"))


# --- GROUP BY --------------------------------------------------------------------


# The group key determines the aggregates however the GROUP BY spells its targets.
# ``GROUP BY 1`` is the dbt house style, so the ordinal carries most real aggregate models.
_GROUP_SPELLINGS: list[tuple[str, str]] = [
    ("expression", "SELECT country, SUM(amount) AS total FROM payments GROUP BY country"),
    ("ordinal", "SELECT country, SUM(amount) AS total FROM payments GROUP BY 1"),
    (
        "ordinal-through-alias",
        "SELECT payments.country AS country, SUM(amount) AS total FROM payments GROUP BY 1",
    ),
]


@pytest.mark.parametrize(
    "sql", [sql for _name, sql in _GROUP_SPELLINGS], ids=[name for name, _sql in _GROUP_SPELLINGS]
)
def test_group_by_determines_the_aggregates(sql: str) -> None:
    out = _fds(_declared(), _source(_PAYMENTS.unique_id), _node("model.shop.by_country", sql))
    assert out["model.shop.by_country"] == FDSet.of(_fd("total", "country"))


def test_group_by_name_shadowed_by_an_input_column_determines_nothing() -> None:
    # `GROUP BY country` binds to the input column, which the projection aliases over, so
    # the grouping is by (country, currency) while the output carries only the renamed
    # currency. Two groups sharing a currency then disagree on the total, so `country` in
    # the output determines nothing. Reading the name as its projection would collapse the
    # two targets onto `currency` and mint the dependency anyway.
    out = _fds(
        _declared(),
        _source(_PAYMENTS.unique_id),
        _node(
            "model.shop.by_country",
            "SELECT p.currency AS country, SUM(amount) AS total FROM payments p "
            "GROUP BY country, p.currency",
        ),
    )
    assert out["model.shop.by_country"] == FDSet.of()


def test_group_by_keeps_fds_among_the_group_columns() -> None:
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _node(
            "model.shop.by_cc",
            "SELECT country, currency, SUM(amount) AS total FROM payments "
            "GROUP BY country, currency",
        ),
    )
    assert out["model.shop.by_cc"] == _carried(
        _inst(_fd("currency", "country")),
        derived=(_fd("total", "country", "currency"),),
    )


def test_group_by_drops_fds_reaching_outside_the_group_key() -> None:
    """``currency`` is aggregated away, so ``country -> currency`` says nothing about
    the output rows and must not survive."""
    out = _fds(
        _declared(_fd("region", "country")),
        _source(_PAYMENTS.unique_id),
        _node(
            "model.shop.by_country",
            "SELECT country, SUM(amount) AS total FROM payments GROUP BY country",
        ),
    )
    assert out["model.shop.by_country"] == FDSet.of(_fd("total", "country"))


def test_star_over_a_group_by_keeps_only_within_group_fds() -> None:
    """``SELECT * ... GROUP BY`` parses even where engines reject it, and the star
    bypasses the projection remap, so the grouping step itself must drop any
    dependency reaching outside the group key: in a permissive dialect each group
    surfaces one arbitrary row, and two groups sharing a determinant value can
    surface different dependents."""
    out = _fds(
        _declared(_fd("region", "country"), _fd("currency", "region")),
        _source(_PAYMENTS.unique_id),
        _node("model.shop.g", "SELECT * FROM payments GROUP BY country"),
    )
    assert out["model.shop.g"] == NO_FDS


# --- the key-derived source -------------------------------------------------------


def test_a_key_determines_the_columns_selected_alongside_it() -> None:
    """A relation unique on ``id`` admits one row per ``id``, so ``id`` determines
    every column drawn from it. Read from the uniqueness property through the
    declared dependency edge."""
    orders = _source("source.shop.raw.orders")
    out = _fds(
        _declared(),
        orders,
        _unique("test.shop.u", column="id", target=orders.unique_id),
        _node("model.shop.stg", "SELECT id, customer_id FROM orders"),
        read_keys=True,
    )
    assert out["model.shop.stg"] == FDSet.of(_fd("customer_id", "id"))


def test_without_the_uniqueness_edge_no_key_fd_is_minted() -> None:
    orders = _source("source.shop.raw.orders")
    out = _fds(
        _declared(),
        orders,
        _unique("test.shop.u", column="id", target=orders.unique_id),
        _node("model.shop.stg", "SELECT id, customer_id FROM orders"),
    )
    assert out["model.shop.stg"] == NO_FDS


# --- dependency through joins (C4) ----------------------------------------------

_CUSTOMERS = SourceRef(SourceKind.SOURCE, "source.shop.raw.customers")


def _declared_on(by_scope: Mapping[SourceRef, tuple[FD, ...]]) -> _FdFacts:
    """Declared FD facts on several sources at once (a join needs each side grounded)."""
    return {
        scope: (
            Fact(
                scope=scope,
                value=FDSet.of(*fds),
                provenance=Declared(DeclaredSource.USER_ASSERTED),
            ),
        )
        for scope, fds in by_scope.items()
    }


def test_inner_join_carries_a_joined_relations_fd() -> None:
    """An FD that holds on one joined relation holds on the inner join: two output
    rows agreeing on the determinant come from that relation's rows agreeing on it,
    and a join only filters or duplicates rows. So ``country -> currency`` declared on
    ``customers`` survives the join."""
    out = _fds(
        _declared_on({_CUSTOMERS: (_fd("currency", "country"),)}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.amount, c.country, c.currency FROM payments p "
            "JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == _carried(_inst(_fd("currency", "country"), origin=_CUSTOMERS))


def test_inner_join_carries_both_sides_fds() -> None:
    out = _fds(
        _declared_on(
            {
                _PAYMENTS: (_fd("amount", "ref"),),
                _CUSTOMERS: (_fd("currency", "country"),),
            }
        ),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.ref, p.amount, c.country, c.currency FROM payments p "
            "JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == _carried(
        _inst(_fd("amount", "ref")),
        _inst(_fd("currency", "country"), origin=_CUSTOMERS),
    )


def test_inner_join_qualifies_under_a_name_collision() -> None:
    """Both sides expose a ``country`` column, but the dependency is the joined
    relation's. The walk must track which side each column came from (qualified by
    alias) rather than blurring the two ``country`` columns, or it would mint a
    dependency off the wrong column."""
    out = _fds(
        _declared_on({_CUSTOMERS: (_fd("currency", "country"),)}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT c.country AS country, c.currency AS currency, p.country AS p_country "
            "FROM payments p JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == _carried(_inst(_fd("currency", "country"), origin=_CUSTOMERS))


def test_left_join_drops_the_optional_sides_fds() -> None:
    """An outer join pads the optional side with NULL on unmatched rows, so a
    dependency on that side need not survive; the conservative posture drops it
    until the NULL semantics are worked through."""
    out = _fds(
        _declared_on({_CUSTOMERS: (_fd("currency", "country"),)}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.amount, c.country, c.currency FROM payments p "
            "LEFT JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == NO_FDS


def test_left_join_carries_the_kept_sides_fds() -> None:
    """The kept side's rows come through un-padded, at worst duplicated, and a
    duplicate still agrees on the dependent: its dependencies survive the outer join."""
    out = _fds(
        _declared_on({_PAYMENTS: (_fd("amount", "ref"),)}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.ref, p.amount, c.country FROM payments p "
            "LEFT JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == _carried(_inst(_fd("amount", "ref")))


def test_left_join_does_not_mint_the_on_equality() -> None:
    """The ON equality holds only on matched rows; a padded row carries NULL on the
    optional side, so no mutual determination is minted for an outer join's keys."""
    out = _fds(
        _declared_on({}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.customer_id, c.id FROM payments p "
            "LEFT JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == NO_FDS


def test_right_join_keeps_the_joined_in_side() -> None:
    """A RIGHT join pads the accumulated left side and keeps the joined-in one, the
    mirror of LEFT: the joined-in relation's dependencies survive, the left's drop."""
    out = _fds(
        _declared_on(
            {
                _PAYMENTS: (_fd("amount", "ref"),),
                _CUSTOMERS: (_fd("currency", "country"),),
            }
        ),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.ref, p.amount, c.country, c.currency FROM payments p "
            "RIGHT JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == _carried(_inst(_fd("currency", "country"), origin=_CUSTOMERS))


def test_full_join_proves_nothing() -> None:
    """A FULL join can pad either side, so neither side's dependencies survive."""
    out = _fds(
        _declared_on(
            {
                _PAYMENTS: (_fd("amount", "ref"),),
                _CUSTOMERS: (_fd("currency", "country"),),
            }
        ),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.ref, p.amount, c.country, c.currency FROM payments p "
            "FULL JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == NO_FDS


def test_cross_join_carries_side_fds() -> None:
    """A cross join pads nothing: it only duplicates rows, and duplicates still agree
    on the dependent, so each side's dependencies survive (there is just no ON to
    mint an equality from)."""
    out = _fds(
        _declared_on({_CUSTOMERS: (_fd("currency", "country"),)}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.amount, c.country, c.currency FROM payments p CROSS JOIN customers c",
        ),
    )
    assert out["model.shop.m"] == _carried(_inst(_fd("currency", "country"), origin=_CUSTOMERS))


def test_a_later_outer_join_pads_an_earlier_inner_joins_equality() -> None:
    """An inner join's ON equality is minted only while both its columns stay on kept
    sides: a later RIGHT join pads the whole accumulated left, taking the earlier
    equality's columns with it."""
    extra = SourceRef(SourceKind.SOURCE, "source.shop.raw.extra")
    out = _fds(
        _declared_on({extra: (_fd("v", "g"),)}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _source(extra.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.customer_id, c.id, e.g, e.v FROM payments p "
            "JOIN customers c ON p.customer_id = c.id "
            "RIGHT JOIN extra e ON c.id = e.k",
        ),
    )
    assert out["model.shop.m"] == _carried(_inst(_fd("v", "g"), origin=extra))


def test_inner_join_carries_a_where_pin_on_either_side() -> None:
    """An equality filter pins its column constant whichever side it sits on."""
    out = _fds(
        _declared_on({}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node(
            "model.shop.m",
            "SELECT p.amount, c.currency FROM payments p "
            "JOIN customers c ON p.customer_id = c.id WHERE c.currency = 'usd'",
        ),
    )
    assert out["model.shop.m"] == FDSet.of(_fd("currency"))


# --- set operations ---------------------------------------------------------------
#
# The union merge keeps exactly the declared instances every arm shares, whole,
# after positional alignment: a union adds only cross pairs (one row per arm), an
# arm-local witness leaves them uncovered, and one shared axiom covers them. The
# carry side is owned by the union PBT in
# test_pbt_functional_dependency_soundness.py, whose anti-vacuity arm spans both
# operators, both arm orders, and differing arm aliases; the rows here pin what
# its generator cannot reach: the two structural carries (chain flattening, the
# dbt CTE-and-rename shell), one drop per soundness edge (derived witness, two
# worlds, an unknown arm, two axioms of one world on the same columns, crossed
# columns, a star arm), and the other set operators, which claim nothing.

_CC = _fd("currency", "country")

_SET_OPERATIONS = [
    pytest.param(
        _declared(_CC),
        "SELECT country, currency FROM payments "
        "UNION ALL SELECT country, currency FROM payments "
        "UNION ALL SELECT country, currency FROM payments",
        _carried(_inst(_CC)),
        id="chained-arms-flatten",
    ),
    pytest.param(
        _declared(_CC),
        "WITH unioned AS (SELECT country, currency FROM payments "
        "UNION ALL SELECT country, currency FROM payments) "
        "SELECT country AS nation, currency AS curr FROM unioned",
        _carried(_inst(_CC, as_fd=_fd("curr", "nation"))),
        id="dbt-shape-cte-then-rename",
    ),
    pytest.param(
        _declared(),
        "SELECT country, currency FROM payments WHERE currency = 'usd' "
        "UNION ALL SELECT country, currency FROM payments WHERE currency = 'eur'",
        NO_FDS,
        id="derived-witness-dies",
    ),
    pytest.param(
        _declared_on({_PAYMENTS: (_CC,), _CUSTOMERS: (_CC,)}),
        "SELECT country, currency FROM payments UNION ALL SELECT country, currency FROM customers",
        NO_FDS,
        id="two-declared-worlds-drop",
    ),
    pytest.param(
        _declared(_CC),
        "SELECT country, currency FROM payments UNION ALL SELECT country, currency FROM customers",
        NO_FDS,
        id="declared-meets-unknown-arm-drops",
    ),
    pytest.param(
        _declared(_fd("b", "a"), _fd("d", "c")),
        "SELECT a AS m, b AS n FROM payments UNION ALL SELECT c AS m, d AS n FROM payments",
        NO_FDS,
        id="two-axioms-of-one-world-on-the-same-columns-drop",
    ),
    pytest.param(
        _declared(_CC),
        "SELECT country, currency FROM payments "
        "UNION ALL SELECT currency AS country, country AS currency FROM payments",
        NO_FDS,
        id="crossed-columns-drop",
    ),
    pytest.param(
        _declared(_CC),
        "SELECT country, currency FROM payments UNION ALL SELECT * FROM payments",
        NO_FDS,
        id="star-arm-leaves-nothing-to-align",
    ),
    pytest.param(
        _declared(_CC),
        "SELECT country, currency FROM payments INTERSECT SELECT country, currency FROM payments",
        NO_FDS,
        id="intersect-claims-nothing",
    ),
    pytest.param(
        _declared(_CC),
        "SELECT country, currency FROM payments EXCEPT SELECT country, currency FROM payments",
        NO_FDS,
        id="except-claims-nothing",
    ),
]


@pytest.mark.parametrize(("facts", "sql", "expected"), _SET_OPERATIONS)
def test_set_operation_merges(facts: _FdFacts, sql: str, expected: FDSet) -> None:
    out = _fds(
        facts,
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _node("model.shop.u", sql),
    )
    assert out["model.shop.u"] == expected


# Only a person's claim about the world mints a grounded instance; the closed
# provenance space is decided per kind (a native constraint holds by the
# warehouse's write path, a compile value is minted by the toolchain).
@pytest.mark.parametrize(
    ("provenance", "expected"),
    [
        pytest.param(Declared(DeclaredSource.USER_ASSERTED), _carried(_inst(_CC)), id="declared"),
        pytest.param(NativeConstraint(enforced_on_write=True), FDSet.of(_CC), id="native"),
        pytest.param(
            CompileValue(origin=CompileOrigin.DBT_VAR, world=BASE_WORLD),
            FDSet.of(_CC),
            id="compile-value",
        ),
    ],
)
def test_only_a_declaration_grounds_an_instance(provenance: Provenance, expected: FDSet) -> None:
    fact = Fact(scope=_PAYMENTS, value=FDSet.of(_CC), provenance=provenance)
    assert functional_dependency_grounding({_PAYMENTS: (fact,)})(_PAYMENTS).value == expected
