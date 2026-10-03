# pyright: reportInvalidTypeForm=false, reportUnusedClass=false, reportGeneralTypeIssues=false
"""A computed projection column never removes the keys its bare columns carry (#315)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dblect.adapters import profile_for_adapter
from dblect.analysis import analyze
from dblect.contracts import ContractSelf, contract
from dblect.manifest import DbtTestMetadata, Node, ResourceType
from dblect.sql import FindingKind
from dblect.types import ModelContract, isolated_registry
from tests._manifest_builders import cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source

_LINES = "source.shop.raw.lines"
_M0 = "model.shop.m0"
_M1 = "model.shop.m1"
_ROLLUP = "SELECT order_id, line_id, SUM(amt) AS s FROM analytics.m0 GROUP BY 1, 2"


_COLUMNS = cols(order_id="VARCHAR", line_id="VARCHAR", amt="BIGINT")


def _fires(extra_projection: str, *, from_clause: str = "raw.lines") -> bool:
    return _fires_over(f"SELECT {extra_projection}, order_id, line_id, amt FROM {from_clause}")


def _k_is_unique() -> Node:
    return _node(
        "test.shop.m0_k_unique",
        kind=ResourceType.OTHER,
        depends_on=frozenset({_M0}),
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": "k"}),
        attached_node=_M0,
    )


def _fires_over(m0: str, *, extra_nodes: tuple[Node, ...] = ()) -> bool:
    """Whether the keyed-grain rollup over model ``m0`` reports ``cross_model_fanout``."""
    manifest = _manifest(
        _source(_LINES, name="lines", columns=_COLUMNS),
        _node(_M0, m0),
        _node(_M1, _ROLLUP),
        *extra_nodes,
    )
    with isolated_registry():

        class Lines(ModelContract):
            dbt_model = "raw.lines"

            @contract
            def pk(self: ContractSelf) -> object:
                return self.key(self.order_id, self.line_id)

        report = analyze(manifest, profile_for_adapter("duckdb"))
    return any(
        lf.finding.kind is FindingKind.CROSS_MODEL_FANOUT and lf.model_unique_id == _M1
        for lf in report.audit.findings
    )


# Expression templates over the key columns; ``{n}`` is the output name. Every one yields exactly
# one output row per input row, so none can remove a key.
_ROW_PRESERVING = [
    "CAST(amt AS BIGINT)",
    "UPPER(line_id)",
    "MD5(line_id)",
    "MD5(CONCAT(order_id, line_id))",
    "SHA256(line_id)",
    "HASH(line_id)",
    "CONCAT(order_id, line_id)",
    "LENGTH(line_id)",
    "UPPER(line_id) || 'z'",
    "CASE WHEN amt > 0 THEN 1 ELSE 0 END",
    "COALESCE(line_id, 'a')",
    "ROW_NUMBER() OVER (ORDER BY line_id)",
    "SUM(amt) OVER (PARTITION BY order_id)",
    "1",
]
# Names that sort before and after the bare key columns: carrier choice must not depend on them.
_NAMES = ["a", "k", "line_id_hash", "z", "_key"]


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("expr", _ROW_PRESERVING)
def test_row_preserving_computed_column_keeps_the_keys(expr: str, name: str) -> None:
    assert not _fires(f"{expr} AS {name}")


@given(
    exprs=st.lists(
        st.tuples(st.sampled_from(_ROW_PRESERVING), st.sampled_from(_NAMES)),
        min_size=1,
        max_size=3,
        unique_by=lambda t: t[1],
    )
)
@settings(max_examples=40, deadline=None)
def test_adding_row_preserving_columns_never_changes_the_verdict(
    exprs: list[tuple[str, str]],
) -> None:
    extra = ", ".join(f"{e} AS {n}" for e, n in exprs)
    assert _fires(extra) == _fires("1 AS unrelated")


def test_computed_column_over_an_upstream_copy_keeps_the_keys() -> None:
    """The bare key columns reach the rollup through a second model that renames nothing."""
    assert not _fires("UPPER(line_id) AS k", from_clause="(SELECT * FROM raw.lines)")


def test_a_unique_column_that_only_reads_the_key_is_not_the_origin_grain() -> None:
    """``k`` is unique and reads ``order_id`` and ``line_id``, yet it also reads ``extra`` from
    a fan-out side, so two rows of one line can differ in ``k``. Uniqueness on ``k`` does not
    witness the line grain, and the rollup double counts."""
    m0 = (
        "SELECT CONCAT(l.order_id, l.line_id, f.extra) AS k, l.order_id, l.line_id, l.amt "
        "FROM raw.lines AS l JOIN raw.fan AS f ON l.order_id = f.order_id"
    )
    fan = _source("source.shop.raw.fan", name="fan", columns=cols("order_id", "extra"))
    assert _fires_over(m0, extra_nodes=(fan, _k_is_unique()))


# A set-returning function in the projection repeats each input row once per element, so the
# bare key columns stop being unique even though they are all projected.
@pytest.mark.parametrize(
    "extra",
    [
        "UNNEST([1, 2]) AS k",
        "UNNEST(['a', 'b']) AS z",
        "GENERATE_SERIES(1, 2) AS k",
    ],
)
def test_set_returning_column_does_not_keep_the_keys(extra: str) -> None:
    assert _fires(extra)


def test_set_returning_function_in_a_nested_select_leaves_the_keys() -> None:
    """The generator runs inside a scalar subquery, one value per row, so the outer rows stay
    one per input row."""
    assert not _fires("(SELECT SUM(x) FROM UNNEST([1, 2]) AS t(x)) AS k")


@pytest.mark.parametrize(
    "m0",
    [
        "SELECT DISTINCT UPPER(line_id) AS k, order_id, line_id, amt FROM raw.lines",
        "SELECT UPPER(line_id) AS k, order_id, line_id, amt FROM raw.lines GROUP BY ALL",
        "SELECT order_id, line_id, SUM(amt) AS amt, MAX(UPPER(line_id)) AS k "
        "FROM raw.lines GROUP BY order_id, line_id",
    ],
)
def test_dedup_and_grouping_keep_the_keys_beside_a_computed_column(m0: str) -> None:
    assert not _fires_over(m0)


def test_grouping_coarser_than_the_key_loses_it_whatever_is_computed() -> None:
    m0 = (
        "SELECT order_id, MAX(line_id) AS line_id, SUM(amt) AS amt, UPPER(order_id) AS k "
        "FROM raw.lines GROUP BY order_id"
    )
    assert _fires_over(m0)


def test_a_window_over_one_key_column_is_not_the_origin_grain() -> None:
    """``ROW_NUMBER() OVER (ORDER BY line_id)`` reads one key column and is unique, yet it is
    no function of ``line_id``: over a fanned-out join it numbers the repeated rows apart, so
    uniqueness on it does not witness the line grain and the rollup double counts."""
    m0 = (
        "SELECT ROW_NUMBER() OVER (ORDER BY l.line_id) AS k, l.order_id, l.line_id, l.amt "
        "FROM raw.lines AS l JOIN raw.fan AS f ON l.order_id = f.order_id"
    )
    fan = _source("source.shop.raw.fan", name="fan", columns=cols("order_id", "extra"))
    assert _fires_over(m0, extra_nodes=(fan, _k_is_unique()))
