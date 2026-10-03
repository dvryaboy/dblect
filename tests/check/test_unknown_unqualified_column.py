"""An unqualified column no source in scope can supply makes the model unbuilt.

The qualified form (``p.nosuch``) already fails qualification with ``Unknown column``.
The unqualified form is the same broken SQL, and is reported the same way when the
decision is exact: every source in the scope has a known column set and none has the
name. When any source's columns are unknown (an undocumented source, a model whose
output holds an unexpanded star) the name may belong to it, so the column stays blind.
"""

from __future__ import annotations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import run_check
from dblect.lineage.builder import build_manifest_graph
from dblect.manifest import Manifest, Node
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source

_DUCKDB = profile_for_adapter("duckdb")
_MODEL = "model.shop.m"
_UNKNOWN = "sqlglot: Unknown column: nosuch"


def _documented(name: str = "p") -> Node:
    return _source(f"source.shop.raw.{name}", name=name, columns=_cols("id", "v"))


def _undocumented(name: str) -> Node:
    return _source(f"source.shop.raw.{name}", name=name)


def _model(sql: str, *, after: str | None = None) -> Node:
    deps: frozenset[str] = frozenset() if after is None else frozenset({after})
    return _node(_MODEL, sql, columns=_cols(x="INT"), depends_on=deps)


def _reasons(manifest: Manifest) -> dict[str, str]:
    return {m.unique_id: m.reason for m in run_check(manifest, _DUCKDB).unbuilt}


def _blind(manifest: Manifest, uid: str = _MODEL) -> int:
    [res] = [r for r in build_manifest_graph(manifest).resolution if r.unique_id == uid]
    return res.blind_columns


@pytest.mark.parametrize(
    "sql",
    [
        "select p.nosuch from raw.p p",
        "select nosuch from raw.p",
        "select id, nosuch from raw.p p",
        "select id from raw.p where nosuch > 1",
        "with c as (select id from raw.p) select nosuch from c",
        "select * from (select id from raw.p) d where nosuch is not null",
    ],
)
def test_a_name_no_known_source_has_is_reported_with_the_qualified_reason(sql: str) -> None:
    assert _reasons(_manifest(_documented(), _model(sql))) == {_MODEL: _UNKNOWN}


@pytest.mark.parametrize(
    "sql",
    [
        "select id from raw.p",
        "select v, id from raw.p",
        "select ID from raw.p",
        "select id as k from raw.p order by k",
        "select id as k from raw.p where k > 1",
        "select id as a, a + 1 as b from raw.p",
        "select v, count(*) as c from raw.p group by v having c > 1",
        "select id from raw.p, unnest(v) as t(e) where e > 1",
        "select * from raw.p cross join lateral (select v as z) l where z > 1",
        "select id from raw.p qualify row_number() over (order by v) = 1",
    ],
)
def test_a_known_column_is_fine(sql: str) -> None:
    manifest = _manifest(_documented(), _model(sql))
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 0


def test_an_undocumented_source_keeps_the_column_blind() -> None:
    # Counterexample at the edge: with no known columns the name may well belong to the source.
    manifest = _manifest(_undocumented("p"), _model("select nosuch from raw.p"))
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 1


def test_a_join_of_known_sources_reports_a_name_none_has() -> None:
    manifest = _manifest(
        _documented("p"),
        _source("source.shop.raw.q", name="q", columns=_cols("id", "w")),
        _model("select nosuch from raw.p a join raw.q b on a.id = b.id"),
    )
    assert _reasons(manifest) == {_MODEL: _UNKNOWN}


def test_a_join_resolves_a_name_that_one_known_source_has() -> None:
    manifest = _manifest(
        _documented("p"),
        _source("source.shop.raw.q", name="q", columns=_cols("id", "w")),
        _model("select w from raw.p a join raw.q b on a.id = b.id"),
    )
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 0


def test_one_undocumented_source_in_scope_keeps_the_column_blind() -> None:
    # Counterexample: p lacks the name, but the undocumented q may have it.
    manifest = _manifest(
        _documented("p"),
        _undocumented("q"),
        _model("select nosuch from raw.p a join raw.q b on a.id = b.id"),
    )
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 1


def test_a_model_with_derived_columns_is_a_known_source() -> None:
    upstream = _node("model.shop.up", "select id from raw.p", columns={})
    manifest = _manifest(
        _undocumented("p"),
        upstream,
        _model("select nosuch from up", after="model.shop.up"),
    )
    assert _reasons(manifest) == {_MODEL: _UNKNOWN}


def test_a_model_with_an_unexpanded_star_is_not_a_known_source() -> None:
    # Counterexample: `*` over an undocumented source hides columns the derivation cannot name,
    # so the one explicit column does not make `up` complete.
    upstream = _node("model.shop.up", "select 1 as a, * from raw.p", columns={})
    manifest = _manifest(
        _undocumented("p"), upstream, _model("select b from up", after="model.shop.up")
    )
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 1


def test_a_correlated_reference_to_the_outer_scope_is_not_unknown() -> None:
    # Counterexample: `v` is absent from q but is the outer relation's column.
    manifest = _manifest(
        _documented("p"),
        _source("source.shop.raw.q", name="q", columns=_cols("k")),
        _model("select (select max(k) from raw.q where k = v) as m from raw.p"),
    )
    assert _reasons(manifest) == {}


def test_a_lambda_parameter_is_not_a_column() -> None:
    manifest = _manifest(
        _source("source.shop.raw.p", name="p", columns=_cols("id", "arr")),
        _model("select list_transform(arr, e -> e + 1) as x from raw.p"),
    )
    assert _reasons(manifest) == {}


def test_a_scope_without_sources_is_not_judged() -> None:
    assert _reasons(_manifest(_model("select nosuch"))) == {}


def test_an_ambiguous_name_is_not_reported_as_unknown() -> None:
    # Counterexample: both sources have `id`, so the name is known, merely ambiguous.
    manifest = _manifest(
        _documented("p"),
        _source("source.shop.raw.q", name="q", columns=_cols("id", "k")),
        _model("select id from raw.p a join raw.q b on a.id = b.k"),
    )
    assert _reasons(manifest) == {}


def test_a_values_source_keeps_the_column_blind() -> None:
    manifest = _manifest(
        _documented("p"),
        _model("select nosuch from raw.p, (values (1)) as t(a)"),
    )
    assert _reasons(manifest) == {}
