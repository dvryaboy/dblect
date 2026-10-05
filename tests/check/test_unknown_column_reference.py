"""A column its relation does not have is an error finding when the relation's columns are
fully known, and only a coverage entry when they are not. The completeness decision is the one
``test_unknown_unqualified_column.py`` pins; this file pins what the check reports from it."""

from __future__ import annotations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.check import CheckFinding, CheckFindingKind, CheckReport, run_check
from dblect.manifest import Catalog, Manifest, Node
from dblect.severity import Severity, severity_of
from tests._manifest_builders import cols as _cols
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node
from tests._manifest_builders import source as _source

_DUCKDB = profile_for_adapter("duckdb")
_MODEL = "model.shop.m"


def _source_with(name: str, *columns: str) -> Node:
    return _source(f"source.shop.raw.{name}", name=name, columns=_cols(*columns))


def _catalogued(manifest: Manifest, **sources: tuple[str, ...]) -> Manifest:
    return manifest.merge_catalog(
        Catalog(
            columns_by_uid={
                f"source.shop.raw.{name}": dict.fromkeys(cols, "INT")
                for name, cols in sources.items()
            }
        )
    )


def _world(sql: str, *, p: tuple[str, ...] | None = ("id", "valid_to")) -> Manifest:
    """Sources ``p``, ``q`` and ``u``, each documented with one column. ``p`` and ``q`` are
    catalogued (``p`` as given, ``None`` leaving it documented only); ``u`` never is."""
    manifest = _manifest(
        _source_with("p", "id"),
        _source_with("q", "k"),
        _source_with("u", "id"),
        _node(_MODEL, sql, columns=_cols(x="INT")),
    )
    catalogued: dict[str, tuple[str, ...]] = {"q": ("k",)}
    if p is not None:
        catalogued["p"] = p
    return _catalogued(manifest, **catalogued)


def _unknown(report: CheckReport) -> list[CheckFinding]:
    return [f for f in report.findings if f.kind is CheckFindingKind.UNKNOWN_COLUMN_REFERENCE]


@pytest.mark.parametrize(
    ("sql", "relation"),
    [
        ("select p.nosuch from raw.p p", "p"),
        ("select nosuch from raw.p p", "p"),
        ("select p.NOSUCH from raw.p p", "p"),
        ("with c as (select id from raw.p) select c.nosuch from c", "c"),
        ("with c as (select id from raw.p) select nosuch from c", "c"),
        ("select d.nosuch from (select id from raw.p) d", "d"),
        # A qualified name concerns only its own relation, so an incomplete join partner is moot.
        ("select a.nosuch from raw.p a join raw.u b on a.id = b.id", "p"),
    ],
)
def test_an_unknown_column_on_a_complete_relation_is_one_error_finding(
    sql: str, relation: str
) -> None:
    report = run_check(_world(sql), _DUCKDB)
    [finding] = _unknown(report)
    assert finding.model_unique_id == _MODEL
    assert severity_of(finding) is Severity.ERROR
    assert finding.line_start > 0
    assert f"'{relation}'" in finding.message
    assert "nosuch" in finding.message.lower()
    # The coverage entry stays: the model was not analyzed. It is not a second finding.
    assert [m.unique_id for m in report.unbuilt] == [_MODEL]
    assert len(report.findings) == 1


def test_the_message_names_the_closest_existing_columns() -> None:
    manifest = _world("select p.valid_from from raw.p p", p=("id", "valid_to", "other"))
    [finding] = _unknown(run_check(manifest, _DUCKDB))
    assert "valid_to" in finding.message
    assert "valid_from" in finding.message


@pytest.mark.parametrize(
    "sql",
    [
        "select p.aaa, p.bbb from raw.p p where p.ccc = 1",
        "select aaa, bbb from raw.p where ccc = 1",
    ],
)
def test_every_unknown_column_in_a_model_is_reported(sql: str) -> None:
    names = sorted(f.column or "" for f in _unknown(run_check(_world(sql), _DUCKDB)))
    assert names == ["aaa", "bbb", "ccc"]


@pytest.mark.parametrize(
    "sql",
    [
        # `u` has no catalog, so the bare name may be its column.
        "select nosuch from raw.p a join raw.u b on a.id = b.id",
        # A projection with no name hides what the derived table holds.
        "select d.nosuch from (select id + 1 from raw.p) d",
        # An enclosing relation without a catalog may own a correlated name.
        "select id from raw.u where exists (select 1 from raw.q where nosuch = 1)",
    ],
)
def test_an_unknown_name_that_an_incomplete_relation_may_own_is_not_a_finding(sql: str) -> None:
    assert _unknown(run_check(_world(sql), _DUCKDB)) == []


def test_a_documented_only_relation_is_not_complete() -> None:
    report = run_check(_world("select p.nosuch from raw.p p", p=None), _DUCKDB)
    assert _unknown(report) == []
    assert [m.unique_id for m in report.unbuilt] == [_MODEL]


@pytest.mark.parametrize(
    ("upstream_sql", "reported"),
    [("select id from raw.p", True), ("select 1 as a, * from raw.v", False)],
)
def test_a_derived_upstream_model_is_complete_unless_it_hides_a_star(
    upstream_sql: str, reported: bool
) -> None:
    manifest = _catalogued(
        _manifest(
            _source_with("p", "id"),
            _source("source.shop.raw.v", name="v"),
            _node("model.shop.up", upstream_sql, columns={}),
            _node(
                _MODEL,
                "select up.nosuch from up",
                columns=_cols(x="INT"),
                depends_on=frozenset({"model.shop.up"}),
            ),
        ),
        p=("id",),
    )
    assert bool(_unknown(run_check(manifest, _DUCKDB))) is reported


def _with_upstream(upstream_sql: str, downstream_sql: str) -> Manifest:
    return _catalogued(
        _manifest(
            _source_with("p", "id"),
            _node("model.shop.up", upstream_sql, columns={}),
            _node(
                _MODEL,
                downstream_sql,
                columns=_cols(x="INT"),
                depends_on=frozenset({"model.shop.up"}),
            ),
        ),
        p=("id",),
    )


@pytest.mark.parametrize(
    "upstream_sql",
    [
        # Output names known only at run time: a column pattern, a struct unnest, a struct
        # expansion, and the multi-column generators.
        "select columns('i.*') from raw.p",
        "select columns(*) from raw.p",
        "select * columns('i.*') from raw.p",
        "select unnest({'a': 1, 'b': 2})",
        "select unnest({'a': 1, 'b': 2}) as s",
        "select (struct_pack(a := id)).* from raw.p",
        "select id, unnest(struct_pack(id := id)) from raw.p",
    ],
)
def test_a_runtime_named_projection_makes_a_model_incomplete(upstream_sql: str) -> None:
    manifest = _with_upstream(upstream_sql, "select up.id from up")
    assert _unknown(run_check(manifest, _DUCKDB)) == []


@pytest.mark.parametrize(
    "upstream_sql",
    ["select unnest([1, 2]) as id", "select id from raw.p", "select count(*) as id from raw.p"],
)
def test_a_named_scalar_projection_leaves_a_model_complete(upstream_sql: str) -> None:
    manifest = _with_upstream(upstream_sql, "select up.nosuch from up")
    assert len(_unknown(run_check(manifest, _DUCKDB))) == 1


@pytest.mark.parametrize(
    "inner",
    ["select columns('i.*') from raw.p", "select unnest({'a': 1, 'b': 2})"],
)
def test_a_runtime_named_cte_is_incomplete(inner: str) -> None:
    report = run_check(_world(f"with c as ({inner}) select c.id from c"), _DUCKDB)
    assert _unknown(report) == []


@pytest.mark.parametrize(
    "sql",
    [
        "select p from raw.p p",
        "select to_json(p) from raw.p p",
        "select to_json(P) from raw.p p",
        "select to_json(p) from raw.p",
        "select to_json(c) from (select id from raw.p) c",
        "select 1 from raw.p p where exists (select 1 from raw.q q where to_json(p) is not null)",
    ],
)
def test_a_relation_name_used_as_a_whole_row_is_not_a_missing_column(sql: str) -> None:
    assert _unknown(run_check(_world(sql), _DUCKDB)) == []


def test_a_name_that_is_no_relation_in_scope_is_still_reported() -> None:
    [finding] = _unknown(run_check(_world("select q from raw.p p"), _DUCKDB))
    assert finding.column == "q"


@pytest.mark.parametrize(
    ("sql", "missing"),
    [
        ("with c(a, b) as (select id, valid_to from raw.p) select c.a, c.b, c.id from c", ["id"]),
        ("with c(a, b) as (select id, valid_to from raw.p) select c.a, c.zzz from c", ["zzz"]),
        ("select d.a, d.id from (select id, valid_to from raw.p) d(a, b)", ["id"]),
        # An alias list shorter than the projection renames only the leading columns.
        (
            "with c(a) as (select id, valid_to from raw.p) select c.a, c.valid_to, c.id from c",
            ["id"],
        ),
        ("with c(a, b) as (select id, valid_to from raw.p) select c.a, c.b from c", []),
    ],
)
def test_a_column_alias_list_names_the_relations_columns(sql: str, missing: list[str]) -> None:
    names = sorted(f.column or "" for f in _unknown(run_check(_world(sql), _DUCKDB)))
    assert names == missing


def test_a_correlated_qualified_reference_is_reported_once() -> None:
    sql = "select id from raw.p p where exists (select 1 from raw.q q where q.k = p.nosuch)"
    [finding] = _unknown(run_check(_world(sql), _DUCKDB))
    assert finding.column == "nosuch"


def test_a_wide_relation_lists_a_capped_set_of_columns() -> None:
    wide = tuple(f"c{i:02d}" for i in range(40))
    manifest = _world("select p.zzzz from raw.p p", p=wide)
    [finding] = _unknown(run_check(manifest, _DUCKDB))
    assert len(finding.message) < 400
    assert " more." in finding.message
