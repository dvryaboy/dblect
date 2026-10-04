"""A bare column that every source in scope lacks makes the model unbuilt, but only when each
source's column set is complete (catalogued, or derived with no unexpanded star)."""

from __future__ import annotations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import run_check
from dblect.lineage.builder import build_manifest_graph
from dblect.manifest import Catalog, Manifest, Node
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source

_DUCKDB = profile_for_adapter("duckdb")
_MODEL = "model.shop.m"
_UNKNOWN = "sqlglot: Unknown column: nosuch"


def _source_with(name: str, *columns: str) -> Node:
    return _source(f"source.shop.raw.{name}", name=name, columns=_cols(*columns))


def _catalogued(manifest: Manifest, **sources: tuple[str, ...]) -> Manifest:
    """``manifest`` with the catalog reporting exactly ``sources``' columns."""
    return manifest.merge_catalog(
        Catalog(
            columns_by_uid={
                f"source.shop.raw.{name}": dict.fromkeys(cols, "INT")
                for name, cols in sources.items()
            }
        )
    )


def _model(sql: str, *, after: str | None = None) -> Node:
    deps: frozenset[str] = frozenset() if after is None else frozenset({after})
    return _node(_MODEL, sql, columns=_cols(x="INT"), depends_on=deps)


def _reasons(manifest: Manifest) -> dict[str, str]:
    return {m.unique_id: m.reason for m in run_check(manifest, _DUCKDB).unbuilt}


def _blind(manifest: Manifest) -> int:
    [res] = [r for r in build_manifest_graph(manifest).resolution if r.unique_id == _MODEL]
    return res.blind_columns


def _p_world(sql: str) -> Manifest:
    return _catalogued(_manifest(_source_with("p", "id"), _model(sql)), p=("id", "v"))


@pytest.mark.parametrize(
    "sql",
    [
        "select p.nosuch from raw.p p",
        "select id, nosuch from raw.p p",
        "select id from raw.p where nosuch > 1",
        "with c as (select id from raw.p) select nosuch from c",
        "select * from (select id from raw.p) d where nosuch is not null",
        # Every enclosing scope is complete and lacks the name too.
        "select id from raw.p where exists (select 1 from raw.p where nosuch = 1)",
    ],
)
def test_a_name_no_complete_source_has_is_reported_with_the_qualified_reason(sql: str) -> None:
    assert _reasons(_p_world(sql)) == {_MODEL: _UNKNOWN}


@pytest.mark.parametrize(
    "sql",
    [
        "select ID from raw.p",
        "select id as k from raw.p where k > 1",
        "select id from raw.p qualify row_number() over (order by v) = 1",
    ],
)
def test_a_known_column_is_fine(sql: str) -> None:
    manifest = _p_world(sql)
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 0


@pytest.mark.parametrize(
    ("documented", "catalogued", "reported"),
    [
        # Docs are not the warehouse: `nosuch` may be a real column, and dropping the model
        # would be the expensive direction.
        (("id",), None, False),
        ((), None, False),
        (("id",), ("id", "v"), True),
    ],
)
def test_a_name_is_reported_only_against_a_catalogued_source(
    documented: tuple[str, ...], catalogued: tuple[str, ...] | None, reported: bool
) -> None:
    manifest = _manifest(_source_with("p", *documented), _model("select nosuch from raw.p"))
    if catalogued is not None:
        manifest = _catalogued(manifest, p=catalogued)
    assert _reasons(manifest) == ({_MODEL: _UNKNOWN} if reported else {})


@pytest.mark.parametrize(
    ("name", "q_catalogued", "reported"),
    [("nosuch", True, True), ("w", True, False), ("nosuch", False, False)],
)
def test_a_join_reports_a_name_only_when_every_source_is_complete_and_lacks_it(
    name: str, q_catalogued: bool, reported: bool
) -> None:
    manifest = _manifest(
        _source_with("p"),
        _source_with("q", "id"),
        _model(f"select {name} from raw.p a join raw.q b on a.id = b.id"),
    )
    manifest = _catalogued(manifest, p=("id", "v"), **({"q": ("id", "w")} if q_catalogued else {}))
    assert _reasons(manifest) == ({_MODEL: _UNKNOWN} if reported else {})


@pytest.mark.parametrize(
    ("upstream_sql", "reported"),
    [
        ("select id from raw.p", True),
        # `*` over an undocumented source hides columns the derivation cannot name.
        ("select 1 as a, * from raw.p", False),
    ],
)
def test_a_derived_model_is_complete_unless_it_hides_a_star(
    upstream_sql: str, reported: bool
) -> None:
    manifest = _manifest(
        _source("source.shop.raw.p", name="p"),
        _node("model.shop.up", upstream_sql, columns={}),
        _model("select nosuch from up", after="model.shop.up"),
    )
    assert _reasons(manifest) == ({_MODEL: _UNKNOWN} if reported else {})


@pytest.mark.parametrize(
    "sql",
    [
        # `v` is absent from q but is the outer relation's column.
        "select (select max(k) from raw.q where k = v) as m from raw.p",
        # A lambda parameter is not a column.
        "select list_transform(v, e -> e + 1) as x from raw.p",
        # A lateral source supplies `z`.
        "select * from raw.p cross join lateral (select v as z) l where z > 1",
        "select nosuch from raw.p, (values (1)) as t(a)",
        # No source at all.
        "select nosuch",
    ],
)
def test_a_scope_that_cannot_be_judged_is_not_reported(sql: str) -> None:
    manifest = _catalogued(
        _manifest(_source_with("p"), _source_with("q"), _model(sql)), p=("id", "v"), q=("k",)
    )
    assert _reasons(manifest) == {}


@pytest.mark.parametrize(
    "sql",
    [
        "select (select max(k) from raw.q where k = closed_at) as x from raw.p",
        "select id as x from raw.p where exists (select 1 from raw.q where k = closed_at)",
    ],
)
def test_a_correlated_name_may_belong_to_an_incomplete_enclosing_source(sql: str) -> None:
    manifest = _catalogued(
        _manifest(_source_with("p", "id"), _source_with("q"), _model(sql)), q=("k",)
    )
    assert _reasons(manifest) == {}


@pytest.mark.parametrize(
    ("adapter", "name"), [("duckdb", "rowid"), ("postgres", "ctid"), ("redshift", "xmin")]
)
def test_an_implicit_column_is_never_reported(adapter: str, name: str) -> None:
    report = run_check(_p_world(f"select {name} from raw.p"), profile_for_adapter(adapter))
    assert not report.unbuilt
