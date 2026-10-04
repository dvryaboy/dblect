# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
"""A count carries the entity it counts, so a declared count of one entity meets a
different inferred one as a domain-type contradiction.

Boundary tests: manifest and contracts in, findings out. A ``COUNT(DISTINCT x)`` of an
entity identifier counts that entity whatever the joins. A non-distinct count counts
the rows of a single, join-free relation, so it counts the entity of that relation's
single-column key. Every other shape makes no claim and stays quiet.
"""

from __future__ import annotations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFindingKind, run_check
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType
from dblect.types import Count, DomainType, Integer, ModelContract, NominalEnum
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_DUCKDB = profile_for_adapter("duckdb")


class Entity(NominalEnum):
    PLAYER = "player"
    ATTRIBUTE_SNAPSHOT = "attribute_snapshot"
    TEAM = "team"


class EntityId(DomainType):
    id: Integer
    entity: Entity


class EntityCount(DomainType):
    n: Count
    entity: Entity


_PA = "source.shop.raw.player_attributes"


def _unique_test(*columns: str) -> Node:
    """A dbt uniqueness test on the source, single-column or composite."""
    if len(columns) == 1:
        metadata = DbtTestMetadata(name="unique", kwargs={"column_name": columns[0]})
    else:
        metadata = DbtTestMetadata(
            name="unique_combination_of_columns",
            kwargs={"combination_of_columns": list(columns)},
        )
    return _node(
        f"test.shop.unique_{'_'.join(columns)}",
        kind=ResourceType.OTHER,
        test_metadata=metadata,
        attached_node=_PA,
    )


def _manifest_for(sql: str, keys: tuple[str, ...] | None) -> Manifest:
    nodes = [
        _node(
            _PA,
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(id="INT", player_api_id="INT", penalties="INT", rating="DECIMAL"),
        ),
        _node(
            "source.shop.raw.team",
            kind=ResourceType.SOURCE,
            sql=None,
            columns=_cols(id="INT", player_api_id="INT"),
        ),
        _node("model.shop.gold", sql=sql, columns=_cols(n="INT")),
    ]
    if keys is not None:
        nodes.append(_unique_test(*keys))
    return _manifest(*nodes)


def _declare(counted: Entity | None) -> None:
    """The source's identifiers (row id = snapshot, player_api_id = player) and,
    when ``counted`` is given, the gold model's declared count of that entity."""

    class PlayerAttributes(ModelContract):
        dbt_model = "player_attributes"
        id: EntityId.refine(entity=Entity.ATTRIBUTE_SNAPSHOT)
        player_api_id: EntityId.refine(entity=Entity.PLAYER).columns(id="player_api_id")

    if counted is not None:
        # Built through the metaclass: the parametrized entity cannot be read from a
        # class body under postponed evaluation.
        type(
            "Gold",
            (ModelContract,),
            {
                "dbt_model": "gold",
                "__annotations__": {"n": EntityCount.refine(entity=counted)},
            },
        )


def _contradictions(
    sql: str, counted: Entity | None, keys: tuple[str, ...] | None = ("id",)
) -> int:
    _declare(counted)
    report = run_check(_manifest_for(sql, keys), _DUCKDB)
    return sum(
        1
        for f in report.findings
        if f.kind is CheckFindingKind.DOMAIN_TYPE_CONTRADICTION
        and f.model_unique_id == "model.shop.gold"
    )


# The entity counted and one other decide every case; a third adds no decision.
_DECLARED = [Entity.PLAYER, Entity.ATTRIBUTE_SNAPSHOT]

_FROM = "FROM player_attributes AS pa"
_JOIN = f"{_FROM} JOIN team AS t ON t.id = pa.id"

# (aggregate, the entity it counts or None for "no claim") over a single keyed relation.
_SINGLE_RELATION: list[tuple[str, Entity | None]] = [
    ("COUNT(DISTINCT pa.player_api_id)", Entity.PLAYER),
    ("COUNT(DISTINCT pa.id)", Entity.ATTRIBUTE_SNAPSHOT),
    ("COUNT(pa.id)", Entity.ATTRIBUTE_SNAPSHOT),
    ("COUNT(*)", Entity.ATTRIBUTE_SNAPSHOT),
    ("COUNT(1)", Entity.ATTRIBUTE_SNAPSHOT),
    ("COUNT(0)", Entity.ATTRIBUTE_SNAPSHOT),
    ("COUNT('x')", Entity.ATTRIBUTE_SNAPSHOT),
    ("COUNT(NULL)", None),  # always 0: counts no rows
    ("COUNT(pa.player_api_id)", None),  # non-key column: the rows are not identified by it
    ("COUNT(DISTINCT pa.rating)", None),  # a magnitude is not an entity
    ("COUNT(DISTINCT pa.player_api_id, pa.id)", None),  # a tuple names no one entity
]


@pytest.mark.parametrize("declared", _DECLARED)
@pytest.mark.parametrize(("agg", "counts"), _SINGLE_RELATION)
def test_a_declared_count_meets_the_entity_the_query_counts(
    agg: str, counts: Entity | None, declared: Entity
) -> None:
    found = _contradictions(f"SELECT {agg} AS n {_FROM}", declared)
    assert found == (1 if counts is not None and counts is not declared else 0)


def test_an_undeclared_count_never_produces_a_finding() -> None:
    assert _contradictions(f"SELECT COUNT(*) AS n {_FROM}", None) == 0


@pytest.mark.parametrize("declared", _DECLARED)
def test_a_grouped_count_counts_the_rows_of_its_relation(declared: Entity) -> None:
    sql = f"SELECT COUNT(*) AS n {_FROM} GROUP BY pa.penalties"
    assert _contradictions(sql, declared) == (declared is not Entity.ATTRIBUTE_SNAPSHOT)


@pytest.mark.parametrize("agg", ["COUNT(*)", "COUNT(1)", "COUNT(pa.id)"])
@pytest.mark.parametrize("declared", _DECLARED)
def test_a_non_distinct_count_after_a_join_makes_no_claim(agg: str, declared: Entity) -> None:
    # The join can repeat or drop rows, so the rows are no longer one per key value.
    assert _contradictions(f"SELECT {agg} AS n {_JOIN}", declared) == 0


@pytest.mark.parametrize("declared", _DECLARED)
def test_a_distinct_count_of_an_entity_identifier_survives_a_join(declared: Entity) -> None:
    sql = f"SELECT COUNT(DISTINCT pa.player_api_id) AS n {_JOIN}"
    assert _contradictions(sql, declared) == (declared is not Entity.PLAYER)


@pytest.mark.parametrize("agg", ["COUNT(*)", "COUNT(pa.id)"])
def test_a_non_distinct_count_over_an_unkeyed_relation_makes_no_claim(agg: str) -> None:
    assert _contradictions(f"SELECT {agg} AS n {_FROM}", Entity.PLAYER, keys=None) == 0


def test_a_composite_key_names_no_single_row_entity() -> None:
    sql = f"SELECT COUNT(*) AS n {_FROM}"
    assert _contradictions(sql, Entity.PLAYER, keys=("id", "player_api_id")) == 0


# A window folds its PARTITION BY columns into the count's tag, so a partitioned window
# widens the claim away; an unpartitioned DISTINCT window keeps it.
@pytest.mark.parametrize(
    ("window", "claims_player"),
    [
        ("COUNT(DISTINCT pa.player_api_id) OVER ()", True),
        ("COUNT(DISTINCT pa.player_api_id) OVER (PARTITION BY pa.penalties)", False),
        ("COUNT(*) OVER ()", False),
        ("COUNT(*) OVER (PARTITION BY pa.penalties)", False),
    ],
)
@pytest.mark.parametrize("declared", _DECLARED)
def test_a_windowed_count_claims_only_an_unpartitioned_distinct_entity(
    window: str, claims_player: bool, declared: Entity
) -> None:
    found = _contradictions(f"SELECT {window} AS n {_FROM}", declared)
    assert found == (1 if claims_player and declared is not Entity.PLAYER else 0)
