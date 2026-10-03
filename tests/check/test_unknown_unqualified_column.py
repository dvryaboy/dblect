"""An unqualified column no source in scope can supply makes the model unbuilt.

The qualified form (``p.nosuch``) already fails qualification with ``Unknown column``.
The unqualified form is the same broken SQL, and is reported the same way when the
decision is exact: every source in the scope has a complete column set and none has the
name. A set is complete when the catalog supplied it, or dblect derived it from a model's
compiled SQL with no unexpanded star. Documented columns alone are a lower bound, so with
any source incomplete the name may belong to it and the column stays blind.
"""

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
    """A model over source ``p``, whose catalog columns are ``id`` and ``v``."""
    return _catalogued(_manifest(_source_with("p", "id"), _model(sql)), p=("id", "v"))


@pytest.mark.parametrize(
    "sql",
    [
        "select p.nosuch from raw.p p",
        "select id, nosuch from raw.p p",
        "select id from raw.p where nosuch > 1",
        "with c as (select id from raw.p) select nosuch from c",
        "select * from (select id from raw.p) d where nosuch is not null",
    ],
)
def test_a_name_no_complete_source_has_is_reported_with_the_qualified_reason(sql: str) -> None:
    assert _reasons(_p_world(sql)) == {_MODEL: _UNKNOWN}


@pytest.mark.parametrize(
    "sql",
    [
        "select ID from raw.p",
        "select id as k from raw.p where k > 1",
        "select * from raw.p cross join lateral (select v as z) l where z > 1",
        "select id from raw.p qualify row_number() over (order by v) = 1",
    ],
)
def test_a_known_column_is_fine(sql: str) -> None:
    manifest = _p_world(sql)
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 0


def test_a_documented_only_source_keeps_the_column_blind() -> None:
    # Counterexample: schema.yml lists `id`, but docs are not the warehouse, so `closed_at`
    # may be a real column. Dropping the model here would be the expensive direction.
    manifest = _manifest(_source_with("p", "id"), _model("select closed_at from raw.p"))
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 1


def test_a_catalogued_source_without_the_name_is_reported() -> None:
    manifest = _catalogued(
        _manifest(_source_with("p", "id"), _model("select closed_at from raw.p")), p=("id", "v")
    )
    assert _reasons(manifest) == {_MODEL: "sqlglot: Unknown column: closed_at"}


def test_an_undocumented_uncatalogued_source_keeps_the_column_blind() -> None:
    manifest = _manifest(_source("source.shop.raw.p", name="p"), _model("select nosuch from raw.p"))
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 1


def test_a_join_reports_a_name_none_of_its_complete_sources_has() -> None:
    manifest = _catalogued(
        _manifest(
            _source_with("p"),
            _source_with("q"),
            _model("select nosuch from raw.p a join raw.q b on a.id = b.id"),
        ),
        p=("id", "v"),
        q=("id", "w"),
    )
    assert _reasons(manifest) == {_MODEL: _UNKNOWN}


def test_a_join_resolves_a_name_that_one_source_has() -> None:
    manifest = _catalogued(
        _manifest(
            _source_with("p"),
            _source_with("q"),
            _model("select w from raw.p a join raw.q b on a.id = b.id"),
        ),
        p=("id", "v"),
        q=("id", "w"),
    )
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 0


def test_one_incomplete_source_in_scope_keeps_the_column_blind() -> None:
    # Counterexample: p is complete and lacks the name, but the documented-only q may have it.
    manifest = _catalogued(
        _manifest(
            _source_with("p"),
            _source_with("q", "id"),
            _model("select nosuch from raw.p a join raw.q b on a.id = b.id"),
        ),
        p=("id", "v"),
    )
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 1


def test_a_model_derived_without_a_star_is_complete_even_when_nothing_is_catalogued() -> None:
    upstream = _node("model.shop.up", "select id from raw.p", columns={})
    manifest = _manifest(
        _source("source.shop.raw.p", name="p"),
        upstream,
        _model("select nosuch from up", after="model.shop.up"),
    )
    assert _reasons(manifest) == {_MODEL: _UNKNOWN}


def test_a_model_with_an_unexpanded_star_is_incomplete() -> None:
    # Counterexample: `*` over an undocumented source hides columns the derivation cannot name.
    upstream = _node("model.shop.up", "select 1 as a, * from raw.p", columns={})
    manifest = _manifest(
        _source("source.shop.raw.p", name="p"),
        upstream,
        _model("select b from up", after="model.shop.up"),
    )
    assert _reasons(manifest) == {}
    assert _blind(manifest) == 1


@pytest.mark.parametrize(
    "sql",
    [
        # `v` is absent from q but is the outer relation's column.
        "select (select max(k) from raw.q where k = v) as m from raw.p",
        # A lambda parameter is not a column.
        "select list_transform(v, e -> e + 1) as x from raw.p",
        # A VALUES source's columns are not read here.
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
