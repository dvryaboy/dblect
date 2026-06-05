"""Property-based tests for the two load-bearing relation-algebra rules.

The scenario tests pin specific shapes; these generalise the two rules whose
soundness the multi-source migration rests on (see ``column-level-lineage.md``):

* **JOIN coverage.** A probe row cannot be multiplied by a joined-in side that is
  unique on the join columns, so the probe side's key survives a JOIN exactly
  when some key of the joined-in side is covered by the equi-join columns.
* **GROUP BY.** Grouping on a set of bare columns makes that set a candidate key
  of the output.

Both run end to end through ``build_relation_graph`` + ``propagate`` so they pin
the contract, not an internal helper.
"""

from __future__ import annotations

from collections.abc import Mapping

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.builder import build_relation_graph
from dblect.lineage.graph import SourceKind
from dblect.lineage.properties.uniqueness import CandidateKeySet, Key, uniqueness_property
from dblect.lineage.property import propagate
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType

_COLS = ("a", "b", "c")


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


def _combo_test(uid: str, *, columns: frozenset[str], target: str) -> Node:
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
        test_metadata=DbtTestMetadata(
            name="dbt_utils.unique_combination_of_columns",
            kwargs={"combination_of_columns": sorted(columns)},
        ),
        attached_node=target,
    )


def _unique_test(uid: str, *, column: str, target: str) -> Node:
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


def _model_keys(*nodes: Node) -> Mapping[str, CandidateKeySet]:
    manifest = Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )
    graph = build_relation_graph(manifest).graph
    anns = propagate(graph, uniqueness_property(manifest))
    return {ref.unique_id: ann.value for ref, ann in anns.items() if ref.kind is SourceKind.MODEL}


# Each right-side key is a non-empty set of columns; the right side carries a set
# of them. The join equates the probe's fk to a single right column.
_right_keys = st.frozensets(
    st.frozensets(st.sampled_from(_COLS), min_size=1, max_size=2), max_size=3
)


@given(right_keys=_right_keys, join_col=st.sampled_from(_COLS))
def test_join_preserves_probe_key_iff_joined_side_is_unique_on_join_columns(
    right_keys: frozenset[Key], join_col: str
) -> None:
    left = _source("source.shop.raw.lft")
    right = _source("source.shop.raw.rgt")
    combo_tests = [
        _combo_test(f"test.shop.rk{i}", columns=key, target=right.unique_id)
        for i, key in enumerate(sorted(right_keys, key=sorted))
    ]
    model = _model(
        "model.shop.j",
        f"SELECT o.id, c.tag FROM lft o LEFT JOIN rgt c ON o.fk = c.{join_col}",
    )
    keys = _model_keys(
        left,
        right,
        _unique_test("test.shop.lid", column="id", target=left.unique_id),
        *combo_tests,
        model,
    )
    # A single equi-join column covers a right key only when that key is exactly
    # {join_col}; then the probe key {id} survives, otherwise no key is proven.
    preserved = frozenset({join_col}) in right_keys
    expected = CandidateKeySet.of(frozenset({"id"})) if preserved else CandidateKeySet.of()
    assert keys["model.shop.j"] == expected


@given(group=st.frozensets(st.sampled_from(_COLS), min_size=1, max_size=3))
def test_group_by_makes_the_group_columns_a_key(group: frozenset[str]) -> None:
    src = _source("source.shop.raw.orders")
    cols = ", ".join(sorted(group))
    model = _model("model.shop.g", f"SELECT {cols}, COUNT(*) AS n FROM orders GROUP BY {cols}")
    keys = _model_keys(src, model)
    assert keys["model.shop.g"] == CandidateKeySet.of(frozenset(group))
