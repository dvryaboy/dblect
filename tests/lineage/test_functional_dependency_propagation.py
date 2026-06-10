"""Relation-scoped functional-dependency propagation, end to end through the substrate.

These pin the FD relation walk at the contract boundary: build a manifest of
sources and models, ground declared dependencies from synthetic facts (the typed
contract bridge is a later build), run the one propagator, and read each
relation's FD set. The rules under test are the sound ones the walk can justify:
a passthrough carries the source's dependencies, a projection renames them and
drops what it cannot carry, a WHERE preserves them and pins filtered columns
constant, a GROUP BY determines every other output from the group key, a key read
from the uniqueness property determines the columns selected alongside it, and a
join or UNION proves nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage.builder import build_relation_graph
from dblect.lineage.facts.model import Annotation, Declared, DeclaredSource, Fact
from dblect.lineage.facts.registry import AnnotationStore, PropertyRegistry
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.functional_dependency import (
    FD,
    NO_FDS,
    FDSet,
    functional_dependency_grounding,
    functional_dependency_property,
)
from dblect.lineage.properties.uniqueness import uniqueness_property
from dblect.lineage.property import propagate
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType

_FdFacts = Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]]


def _model(uid: str, sql: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code=sql,
        original_file_path=None,
        columns={},
    )


def _source(uid: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.SOURCE,
        fqn=(uid,),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _unique(uid: str, *, column: str, target: str) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.OTHER,
        fqn=(uid,),
        package_name="shop",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
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


def _fds(facts: _FdFacts, *nodes: Node, read_keys: bool = False) -> dict[str, FDSet]:
    """Build a manifest from the nodes, propagate the FD property (after uniqueness
    when ``read_keys`` is set, so the key-derived source is live), and return each
    model's FD set keyed by unique_id."""
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )
    graph = build_relation_graph(manifest).graph
    ground = functional_dependency_grounding(facts)
    if read_keys:
        uniq = uniqueness_property(manifest)
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
        _model("model.shop.stg", "SELECT country, currency, amount FROM payments"),
    )
    assert out["model.shop.stg"] == FDSet.of(_fd("currency", "country"))


def test_projection_renames_both_sides() -> None:
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _model("model.shop.stg", "SELECT country AS nation, currency AS curr FROM payments"),
    )
    assert out["model.shop.stg"] == FDSet.of(_fd("curr", "nation"))


def test_dropping_a_dependency_column_drops_the_fd() -> None:
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _model("model.shop.stg", "SELECT country, amount FROM payments"),
    )
    assert out["model.shop.stg"] == NO_FDS


def test_star_carries_everything() -> None:
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _model("model.shop.stg", "SELECT * FROM payments"),
    )
    assert out["model.shop.stg"] == FDSet.of(_fd("currency", "country"))


# --- WHERE ----------------------------------------------------------------------


def test_where_preserves_fds() -> None:
    """A filter removes rows, and a dependency that holds on all rows holds on any
    subset, so the FD survives."""
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _model("model.shop.stg", "SELECT country, currency FROM payments WHERE amount > 0"),
    )
    assert out["model.shop.stg"] == FDSet.of(_fd("currency", "country"))


def test_where_equality_pins_a_column_constant() -> None:
    """``WHERE currency = 'usd'`` makes ``currency`` single-valued over the result,
    which is the empty-determinant dependency."""
    out = _fds(
        _declared(),
        _source(_PAYMENTS.unique_id),
        _model("model.shop.usd", "SELECT country, currency FROM payments WHERE currency = 'usd'"),
    )
    assert out["model.shop.usd"] == FDSet.of(_fd("currency"))


def test_constancy_flows_through_a_cte_and_a_downstream_model() -> None:
    out = _fds(
        _declared(),
        _source(_PAYMENTS.unique_id),
        _model(
            "model.shop.usd",
            "WITH f AS (SELECT country, currency FROM payments WHERE currency = 'usd') "
            "SELECT country, currency FROM f",
        ),
        _model("model.shop.downstream", "SELECT country, currency FROM usd"),
    )
    assert out["model.shop.usd"] == FDSet.of(_fd("currency"))
    assert out["model.shop.downstream"] == FDSet.of(_fd("currency"))


# --- GROUP BY --------------------------------------------------------------------


def test_group_by_determines_the_aggregates() -> None:
    out = _fds(
        _declared(),
        _source(_PAYMENTS.unique_id),
        _model(
            "model.shop.by_country",
            "SELECT country, SUM(amount) AS total FROM payments GROUP BY country",
        ),
    )
    assert out["model.shop.by_country"] == FDSet.of(_fd("total", "country"))


def test_group_by_keeps_fds_among_the_group_columns() -> None:
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _model(
            "model.shop.by_cc",
            "SELECT country, currency, SUM(amount) AS total FROM payments "
            "GROUP BY country, currency",
        ),
    )
    assert out["model.shop.by_cc"] == FDSet.of(
        _fd("currency", "country"),
        _fd("total", "country", "currency"),
    )


def test_group_by_drops_fds_reaching_outside_the_group_key() -> None:
    """``currency`` is aggregated away, so ``country -> currency`` says nothing about
    the output rows and must not survive."""
    out = _fds(
        _declared(_fd("region", "country")),
        _source(_PAYMENTS.unique_id),
        _model(
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
        _model("model.shop.g", "SELECT * FROM payments GROUP BY country"),
    )
    assert out["model.shop.g"] == NO_FDS


# --- shapes the walk does not model ----------------------------------------------


def test_union_all_proves_nothing() -> None:
    """Two arms can each satisfy a dependency while their union violates it (the same
    determinant value mapping to different dependents per arm)."""
    out = _fds(
        _declared(_fd("currency", "country")),
        _source(_PAYMENTS.unique_id),
        _model(
            "model.shop.u",
            "SELECT country, currency FROM payments "
            "UNION ALL SELECT country, currency FROM payments",
        ),
    )
    assert out["model.shop.u"] == NO_FDS


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
        _model("model.shop.stg", "SELECT id, customer_id FROM orders"),
        read_keys=True,
    )
    assert out["model.shop.stg"] == FDSet.of(_fd("customer_id", "id"))


def test_without_the_uniqueness_edge_no_key_fd_is_minted() -> None:
    orders = _source("source.shop.raw.orders")
    out = _fds(
        _declared(),
        orders,
        _unique("test.shop.u", column="id", target=orders.unique_id),
        _model("model.shop.stg", "SELECT id, customer_id FROM orders"),
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
        _model(
            "model.shop.m",
            "SELECT p.amount, c.country, c.currency FROM payments p "
            "JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == FDSet.of(_fd("currency", "country"))


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
        _model(
            "model.shop.m",
            "SELECT p.ref, p.amount, c.country, c.currency FROM payments p "
            "JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == FDSet.of(_fd("amount", "ref"), _fd("currency", "country"))


def test_inner_join_qualifies_under_a_name_collision() -> None:
    """Both sides expose a ``country`` column, but the dependency is the joined
    relation's. The walk must track which side each column came from (qualified by
    alias) rather than blurring the two ``country`` columns, or it would mint a
    dependency off the wrong column."""
    out = _fds(
        _declared_on({_CUSTOMERS: (_fd("currency", "country"),)}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _model(
            "model.shop.m",
            "SELECT c.country AS country, c.currency AS currency, p.country AS p_country "
            "FROM payments p JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == FDSet.of(_fd("currency", "country"))


def test_left_join_proves_nothing() -> None:
    """An outer join pads the optional side with NULL on unmatched rows, so a
    dependency on that side need not survive; the conservative posture proves
    nothing until the NULL semantics are worked through."""
    out = _fds(
        _declared_on({_CUSTOMERS: (_fd("currency", "country"),)}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _model(
            "model.shop.m",
            "SELECT p.amount, c.country, c.currency FROM payments p "
            "LEFT JOIN customers c ON p.customer_id = c.id",
        ),
    )
    assert out["model.shop.m"] == NO_FDS


def test_cross_join_proves_nothing() -> None:
    out = _fds(
        _declared_on({_CUSTOMERS: (_fd("currency", "country"),)}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _model(
            "model.shop.m",
            "SELECT p.amount, c.country, c.currency FROM payments p CROSS JOIN customers c",
        ),
    )
    assert out["model.shop.m"] == NO_FDS


def test_inner_join_carries_a_where_pin_on_either_side() -> None:
    """An equality filter pins its column constant whichever side it sits on."""
    out = _fds(
        _declared_on({}),
        _source(_PAYMENTS.unique_id),
        _source(_CUSTOMERS.unique_id),
        _model(
            "model.shop.m",
            "SELECT p.amount, c.currency FROM payments p "
            "JOIN customers c ON p.customer_id = c.id WHERE c.currency = 'usd'",
        ),
    )
    assert out["model.shop.m"] == FDSet.of(_fd("currency"))
