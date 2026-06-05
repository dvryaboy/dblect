"""Predicate-flow propagation: the row filter every row of a relation satisfies.

These pin the relation algebra at the contract boundary: build a manifest of
sources and models, run the one propagator with the predicate-flow property, and
read each relation's accumulated row filter. The rules under test are the sound
ones: a ``WHERE`` conjoins, a passthrough carries the upstream filter, a consumer
adds its own filter, a projection renames the filter's columns, and the shapes
where row identity changes or the columns blur (``JOIN``, ``UNION``, ``GROUP BY``,
a dropped column) drop conservatively to "no filter known".
"""

from __future__ import annotations

from collections.abc import Mapping

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.builder import build_relation_graph
from dblect.lineage.graph import SourceKind
from dblect.lineage.predicate import Canon, atoms_of, parse_predicate
from dblect.lineage.properties.predicate_flow import RowFilter, predicate_flow_property
from dblect.lineage.property import propagate
from dblect.manifest import Manifest, Node, ResourceType


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
        constraints=(),
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


def _flow(*nodes: Node) -> Mapping[str, RowFilter]:
    """Build a manifest, propagate predicate-flow, and return each model's filter
    keyed by unique_id."""
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )
    result = build_relation_graph(manifest)
    anns = propagate(result.graph, predicate_flow_property())
    return {ref.unique_id: ann.value for ref, ann in anns.items() if ref.kind is SourceKind.MODEL}


def _atoms(sql: str) -> frozenset[Canon]:
    """The atom set a predicate string carries, the form the flow value holds."""
    parsed = parse_predicate(sql, dialect="duckdb")
    assert parsed is not None, f"could not parse predicate: {sql!r}"
    return atoms_of(parsed)


_ORDERS = "source.shop.raw.orders"


# --- the filter forms ------------------------------------------------------------


def test_passthrough_without_a_filter_knows_nothing() -> None:
    flow = _flow(_source(_ORDERS), _model("model.shop.m", "SELECT * FROM orders"))
    assert flow["model.shop.m"].atoms == frozenset()


def test_where_becomes_the_filter() -> None:
    flow = _flow(
        _source(_ORDERS), _model("model.shop.m", "SELECT * FROM orders WHERE country = 'US'")
    )
    assert flow["model.shop.m"].atoms == _atoms("country = 'US'")


def test_in_filter_carries() -> None:
    flow = _flow(
        _source(_ORDERS),
        _model("model.shop.m", "SELECT * FROM orders WHERE region IN ('US', 'CA')"),
    )
    assert flow["model.shop.m"].atoms == _atoms("region IN ('US', 'CA')")


def test_bare_boolean_carries_under_a_star() -> None:
    # A bare boolean is opaque to interval reasoning, but under ``SELECT *`` every
    # column passes through unchanged, so the atom is carried verbatim.
    flow = _flow(_source(_ORDERS), _model("model.shop.m", "SELECT * FROM orders WHERE active"))
    assert flow["model.shop.m"].atoms == _atoms("active")


# --- flow across models ----------------------------------------------------------


def test_filter_flows_downstream_through_a_passthrough() -> None:
    flow = _flow(
        _source(_ORDERS),
        _model("model.shop.a", "SELECT * FROM orders WHERE country = 'US'"),
        _model("model.shop.b", "SELECT * FROM a"),
    )
    assert flow["model.shop.b"].atoms == _atoms("country = 'US'")


def test_consumer_conjoins_its_own_filter() -> None:
    flow = _flow(
        _source(_ORDERS),
        _model("model.shop.a", "SELECT * FROM orders WHERE country = 'US'"),
        _model("model.shop.b", "SELECT * FROM a WHERE amount > 0"),
    )
    assert flow["model.shop.b"].atoms == _atoms("country = 'US' AND amount > 0")


# --- projection rename and drop --------------------------------------------------


def test_projection_renames_the_filter_columns() -> None:
    flow = _flow(
        _source(_ORDERS),
        _model("model.shop.m", "SELECT country AS region, amount FROM orders WHERE country = 'US'"),
    )
    assert flow["model.shop.m"].atoms == _atoms("region = 'US'")


def test_filter_on_a_dropped_column_is_lost() -> None:
    # ``country`` is filtered but not projected (no star), so the atom has no image
    # in the output columns and drops.
    flow = _flow(
        _source(_ORDERS),
        _model("model.shop.m", "SELECT amount FROM orders WHERE country = 'US'"),
    )
    assert flow["model.shop.m"].atoms == frozenset()


# --- shapes that drop conservatively ---------------------------------------------


def test_join_drops_the_filter() -> None:
    flow = _flow(
        _source(_ORDERS),
        _source("source.shop.raw.customers"),
        _model(
            "model.shop.m",
            "SELECT * FROM orders o JOIN customers c ON o.cid = c.id WHERE o.country = 'US'",
        ),
    )
    assert flow["model.shop.m"].atoms == frozenset()


def test_group_by_drops_the_filter() -> None:
    flow = _flow(
        _source(_ORDERS),
        _model(
            "model.shop.m",
            "SELECT country, count(*) AS n FROM orders WHERE country = 'US' GROUP BY country",
        ),
    )
    assert flow["model.shop.m"].atoms == frozenset()


def test_union_drops_the_filter() -> None:
    flow = _flow(
        _source(_ORDERS),
        _source("source.shop.raw.archive"),
        _model(
            "model.shop.m",
            "SELECT * FROM orders WHERE country = 'US' "
            "UNION ALL SELECT * FROM archive WHERE country = 'US'",
        ),
    )
    assert flow["model.shop.m"].atoms == frozenset()


# --- CTEs and inline subqueries accumulate for free ------------------------------


def test_cte_accumulates_the_filter() -> None:
    flow = _flow(
        _source(_ORDERS),
        _model(
            "model.shop.m",
            "WITH us AS (SELECT * FROM orders WHERE country = 'US') SELECT * FROM us",
        ),
    )
    assert flow["model.shop.m"].atoms == _atoms("country = 'US'")


def test_inline_subquery_accumulates_the_filter() -> None:
    flow = _flow(
        _source(_ORDERS),
        _model(
            "model.shop.m",
            "SELECT * FROM (SELECT * FROM orders WHERE country = 'US') s",
        ),
    )
    assert flow["model.shop.m"].atoms == _atoms("country = 'US'")


def test_cte_filter_conjoins_with_the_outer_filter() -> None:
    flow = _flow(
        _source(_ORDERS),
        _model(
            "model.shop.m",
            "WITH us AS (SELECT * FROM orders WHERE country = 'US') "
            "SELECT * FROM us WHERE amount > 0",
        ),
    )
    assert flow["model.shop.m"].atoms == _atoms("country = 'US' AND amount > 0")


# --- accumulation invariant (PBT) ------------------------------------------------


@given(
    st.lists(
        st.tuples(st.sampled_from((">", "<", ">=", "<=", "=")), st.integers(-3, 3)),
        min_size=1,
        max_size=5,
    )
)
def test_filter_accumulates_down_a_passthrough_chain(specs: list[tuple[str, int]]) -> None:
    """A chain of ``SELECT * FROM prev WHERE <atom>`` models accumulates the union
    of every link's atom: a star passthrough neither renames nor drops, so the
    bottom model's filter is exactly the conjunction of the whole chain."""
    src = _source("source.shop.raw.s")
    atom_sqls = [f"c{i} {op} {lit}" for i, (op, lit) in enumerate(specs)]
    models: list[Node] = []
    prev = "s"
    for i, atom_sql in enumerate(atom_sqls):
        uid = f"model.shop.m{i}"
        models.append(_model(uid, f"SELECT * FROM {prev} WHERE {atom_sql}"))
        prev = f"m{i}"
    flow = _flow(src, *models)
    expected: frozenset[Canon] = frozenset[Canon]().union(*(_atoms(s) for s in atom_sqls))
    assert flow[f"model.shop.m{len(specs) - 1}"].atoms == expected
