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
