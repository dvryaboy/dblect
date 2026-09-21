"""The shared walk-resolve-locate-sort helper every located check finding uses.

Pinned directly (not through a specific detector) so the contract survives a
detector's own refactor: a reader yields rows, ``locate_findings`` resolves each
row's file, compiled span, and back-mapped source span, and sorts the result.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.audit.sourcemap import LineMap
from dblect.check.findings import CheckFindingKind
from dblect.check.located import (
    LocatedRow,
    annotation_or_grounded,
    file_of,
    locate_findings,
    span_of,
)
from dblect.lineage.facts.model import Annotation
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.sql import parse_sql
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.orders")
_COL = ColumnRef(_SRC, "amount")


# --- annotation_or_grounded ------------------------------------------------------


def _ground(_scope: ColumnRef) -> Annotation[str]:
    return Annotation("grounded")


def test_annotation_or_grounded_prefers_the_propagated_value() -> None:
    flowed: Mapping[ColumnRef, Annotation[str]] = {_COL: Annotation("flowed")}
    get = annotation_or_grounded(flowed, ground=_ground)
    assert get(_COL).value == "flowed"


def test_annotation_or_grounded_falls_back_when_the_walk_never_reached_the_scope() -> None:
    empty: Mapping[ColumnRef, Annotation[str]] = {}
    get = annotation_or_grounded(empty, ground=_ground)
    assert get(_COL).value == "grounded"


# --- span_of ---------------------------------------------------------------------


def test_span_of_reads_the_first_usable_line() -> None:
    tree = parse_sql("SELECT a, b FROM t")
    assert span_of(tree.expressions[0]) != (0, 0)


def test_span_of_falls_back_through_its_arguments() -> None:
    tree = parse_sql("SELECT a FROM t")
    span = span_of(None, tree)
    assert span == span_of(tree)


def test_span_of_with_no_usable_line_is_the_unlocated_sentinel() -> None:
    assert span_of(None, None) == (0, 0)


# --- file_of -----------------------------------------------------------------


def test_file_of_reads_the_models_original_file_path() -> None:
    node = _node("model.shop.m", "SELECT 1", path="models/m.sql")
    manifest = _manifest(node)
    assert file_of(manifest, "model.shop.m") == "models/m.sql"


def test_file_of_is_none_for_an_absent_model() -> None:
    assert file_of(_manifest(), "model.shop.missing") is None


# --- locate_findings ---------------------------------------------------------


def _row(uid: str, *, column: str | None, line: int | None) -> LocatedRow:
    tree = parse_sql(f"SELECT {column or 'x'} FROM t")
    node = tree.expressions[0] if line else None
    return LocatedRow(
        uid=uid,
        nodes=(node,),
        kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
        message=f"finding for {column}",
        column=column,
    )


def test_locate_findings_resolves_file_and_span() -> None:
    model = _node("model.shop.m", "SELECT amount FROM t", path="models/m.sql")
    manifest = _manifest(model)
    rows = [_row("model.shop.m", column="amount", line=1)]
    findings = locate_findings(manifest, rows, line_maps={}, sort_key=lambda f: (f.column or "",))
    assert len(findings) == 1
    found = findings[0]
    assert found.model_unique_id == "model.shop.m"
    assert found.file_path == "models/m.sql"
    assert found.column == "amount"
    assert found.line_start != 0


def test_locate_findings_unlocatable_row_gets_the_zero_span() -> None:
    model = _node("model.shop.m", "SELECT amount FROM t", path="models/m.sql")
    manifest = _manifest(model)
    rows = [_row("model.shop.m", column="amount", line=None)]
    findings = locate_findings(manifest, rows, line_maps={}, sort_key=lambda f: (f.column or "",))
    assert findings[0].line_start == 0
    assert findings[0].line_end == 0


def test_locate_findings_sorts_by_the_given_key() -> None:
    model = _node("model.shop.m", "SELECT a FROM t", path="models/m.sql")
    manifest = _manifest(model)
    rows = [
        _row("model.shop.m", column="b", line=1),
        _row("model.shop.m", column="a", line=1),
    ]
    findings = locate_findings(manifest, rows, line_maps={}, sort_key=lambda f: (f.column or "",))
    assert [f.column for f in findings] == ["a", "b"]


def test_locate_findings_shares_one_line_map_per_model() -> None:
    """Two rows from the same model share a cache entry rather than each building
    its own line map, the point of the caller-supplied ``line_maps`` dict."""
    model = _node("model.shop.m", "SELECT a, b FROM t", path="models/m.sql")
    manifest = _manifest(model)
    rows = [
        _row("model.shop.m", column="a", line=1),
        _row("model.shop.m", column="b", line=1),
    ]
    cache: dict[str, LineMap] = {}
    locate_findings(manifest, rows, line_maps=cache, sort_key=lambda f: (f.column or "",))
    assert len(cache) == 1


def test_locate_findings_empty_reader_yields_no_findings() -> None:
    manifest = _manifest()
    assert locate_findings(manifest, [], line_maps={}, sort_key=lambda f: (f.column or "",)) == []
