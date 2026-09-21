"""The shared walk-resolve-locate-sort helper every located check finding uses.

Pinned directly (not through a specific detector) so the contract survives a
detector's own refactor: a reader yields rows, ``locate_findings`` resolves each
row's file, compiled span, and back-mapped source span, and sorts the result.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.check.findings import CheckFindingKind
from dblect.check.located import LocatedRow, annotation_or_grounded, locate_findings
from dblect.lineage.facts.model import Annotation
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.sql import parse_sql
from tests._manifest_builders import manifest as _manifest
from tests._manifest_builders import node as _node

_SRC = SourceRef(SourceKind.SOURCE, "source.shop.raw.orders")
_COL = ColumnRef(_SRC, "amount")


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


def _row(uid: str, *, column: str, located: bool) -> LocatedRow:
    tree = parse_sql(f"SELECT {column} FROM t")
    node = tree.expressions[0] if located else None
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
    rows = [_row("model.shop.m", column="amount", located=True)]
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
    rows = [_row("model.shop.m", column="amount", located=False)]
    findings = locate_findings(manifest, rows, line_maps={}, sort_key=lambda f: (f.column or "",))
    assert (findings[0].line_start, findings[0].line_end) == (0, 0)


def test_locate_findings_sorts_by_the_given_key() -> None:
    model = _node("model.shop.m", "SELECT a FROM t", path="models/m.sql")
    manifest = _manifest(model)
    rows = [
        _row("model.shop.m", column="b", located=True),
        _row("model.shop.m", column="a", located=True),
    ]
    findings = locate_findings(manifest, rows, line_maps={}, sort_key=lambda f: (f.column or "",))
    assert [f.column for f in findings] == ["a", "b"]
