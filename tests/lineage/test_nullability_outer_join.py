"""Outer-join optional-side NULLABLE taint at the annotation boundary.

An outer join pads its optional side with NULL on unmatched rows, so a column drawn from
that side is nullable downstream even when it is NON_NULL at its own source. These pin the
contract: which side reads NULLABLE per join type, that the required side keeps a proven
NON_NULL (precision, so detectors stay quiet there), that the taint rides across a model
boundary (the headline, since the downstream SELECT shows a plain column with no local cue),
that it reaches a column fed by a derived table, and that a guard clears it back to NON_NULL.
The empirical companion (``test_pbt_nullability_soundness``) checks the same against
materialized rows, where these pin the precise per-side outcome the data check cannot name.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.nullability import Nullability, activated_nullability
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType


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


def _not_null(uid: str, *, column: str, target: str) -> Node:
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
        test_metadata=DbtTestMetadata(name="not_null", kwargs={"column_name": column}),
        attached_node=target,
    )


def _activated(*nodes: Node) -> Mapping[ColumnRef, Nullability]:
    manifest = Manifest(
        schema_version="v12",
        adapter_type="duckdb",
        nodes={n.unique_id: n for n in nodes},
    )
    return {ref: ann.value for ref, ann in activated_nullability(manifest).items()}


def _col(uid: str, col: str) -> ColumnRef:
    return ColumnRef(SourceRef(SourceKind.MODEL, uid), col)


# Both source columns carry a ``not_null`` test, so each is NON_NULL at its source. The
# outer join is then the only thing that can introduce a null, which is exactly what these
# isolate: a NULLABLE result is the taint beating a proven NON_NULL, not mere absence of a
# declaration.
_BASE = _source("source.shop.raw.base")
_JOINED = _source("source.shop.raw.joined")
_NN_BASE = _not_null("test.nn_base", column="bk", target="source.shop.raw.base")
_NN_JOINED = _not_null("test.nn_joined", column="jv", target="source.shop.raw.joined")


def _join_model(join: str) -> Node:
    return _model(
        "model.shop.m",
        f"SELECT b.bk AS bk, j.jv AS jv FROM base b {join} joined j ON b.bk = j.jk",
    )


def test_left_join_taints_only_the_joined_in_side() -> None:
    res = _activated(_BASE, _JOINED, _NN_BASE, _NN_JOINED, _join_model("LEFT JOIN"))
    assert res[_col("model.shop.m", "bk")] is Nullability.NON_NULL
    assert res[_col("model.shop.m", "jv")] is Nullability.NULLABLE


def test_right_join_taints_the_accumulated_left_side() -> None:
    res = _activated(_BASE, _JOINED, _NN_BASE, _NN_JOINED, _join_model("RIGHT JOIN"))
    assert res[_col("model.shop.m", "bk")] is Nullability.NULLABLE
    assert res[_col("model.shop.m", "jv")] is Nullability.NON_NULL


def test_full_join_taints_both_sides() -> None:
    res = _activated(_BASE, _JOINED, _NN_BASE, _NN_JOINED, _join_model("FULL JOIN"))
    assert res[_col("model.shop.m", "bk")] is Nullability.NULLABLE
    assert res[_col("model.shop.m", "jv")] is Nullability.NULLABLE


def test_inner_join_taints_nothing() -> None:
    res = _activated(_BASE, _JOINED, _NN_BASE, _NN_JOINED, _join_model("INNER JOIN"))
    assert res[_col("model.shop.m", "bk")] is Nullability.NON_NULL
    assert res[_col("model.shop.m", "jv")] is Nullability.NON_NULL


def test_taint_rides_across_a_model_boundary() -> None:
    # The LEFT JOIN lives in ``stg``; ``dim`` selects plain column names with no local cue
    # that ``jv`` was ever optional. The taint must ride the boundary for ``dim.jv`` to read
    # NULLABLE, while ``dim.bk`` (the required side) keeps its proven NON_NULL.
    res = _activated(
        _BASE,
        _JOINED,
        _NN_BASE,
        _NN_JOINED,
        _model(
            "model.shop.stg",
            "SELECT b.bk AS bk, j.jv AS jv FROM base b LEFT JOIN joined j ON b.bk = j.jk",
        ),
        _model("model.shop.dim", "SELECT bk, jv FROM stg"),
    )
    assert res[_col("model.shop.dim", "bk")] is Nullability.NON_NULL
    assert res[_col("model.shop.dim", "jv")] is Nullability.NULLABLE


def test_taint_reaches_a_column_drawn_from_a_derived_table() -> None:
    # The optional side is an aliased derived table rather than a bare relation; its alias
    # still qualifies ``s.jv`` downstream, so the taint finds it.
    res = _activated(
        _BASE,
        _JOINED,
        _NN_BASE,
        _NN_JOINED,
        _model(
            "model.shop.m",
            "SELECT b.bk AS bk, s.jv AS jv "
            "FROM base b LEFT JOIN (SELECT jk, jv FROM joined) s ON b.bk = s.jk",
        ),
    )
    assert res[_col("model.shop.m", "bk")] is Nullability.NON_NULL
    assert res[_col("model.shop.m", "jv")] is Nullability.NULLABLE


def test_coalesce_guard_clears_the_taint() -> None:
    # COALESCE over the optional-side column restores NON_NULL: the guard's rule runs over
    # the tainted child and a non-null fallback wins, so a healthy model stays silent.
    res = _activated(
        _BASE,
        _JOINED,
        _NN_BASE,
        _NN_JOINED,
        _model(
            "model.shop.m",
            "SELECT b.bk AS bk, COALESCE(j.jv, b.bk) AS jv "
            "FROM base b LEFT JOIN joined j ON b.bk = j.jk",
        ),
    )
    assert res[_col("model.shop.m", "jv")] is Nullability.NON_NULL
