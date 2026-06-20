"""Unit tests for the ``--diff`` change-line filter, with no git involved.

Two pure surfaces are pinned here: ``parse_unified_diff`` (the changed-line map
extracted from ``git diff --unified=0`` text) and ``filter_to_changed_lines`` (the
intersection of structural findings with that map). The end-to-end git behaviour
lives in ``tests/cli/test_check_diff.py``; keeping the pure logic here lets the
contracts be exercised deterministically.
"""

from __future__ import annotations

from dblect.audit.walker import LocatedFinding
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.diff_filter import (
    ChangedLines,
    filter_to_changed_lines,
    parse_unified_diff,
)
from dblect.sql.patterns import Finding, FindingKind


def _structural(path: str | None, *, line_start: int, line_end: int) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id="model.p.m",
        file_path=path,
        finding=Finding(
            kind=FindingKind.UNORDERED_RANKING_WINDOW,
            message="x",
            sql_snippet="row_number()",
            line_start=line_start,
            line_end=line_end,
        ),
    )


# --- parse_unified_diff ------------------------------------------------------


def test_parse_added_lines_single_hunk() -> None:
    text = (
        "diff --git a/models/a.sql b/models/a.sql\n"
        "--- a/models/a.sql\n"
        "+++ b/models/a.sql\n"
        "@@ -3,0 +4,2 @@\n"
        "+select 1\n"
        "+from t\n"
    )
    changed = parse_unified_diff(text)
    assert changed["models/a.sql"] == frozenset({4, 5})


def test_parse_single_added_line_omits_count() -> None:
    # `@@ -1 +2 @@` with no comma means a one-line span.
    text = "diff --git a/m.sql b/m.sql\n--- a/m.sql\n+++ b/m.sql\n@@ -1 +2 @@\n+changed\n"
    assert parse_unified_diff(text)["m.sql"] == frozenset({2})


def test_parse_pure_deletion_contributes_no_new_lines() -> None:
    # `+0,0` means nothing was added on the new side; a deletion-only hunk leaves
    # no reachable changed line on the post-image, so the file maps to an empty set.
    text = (
        "diff --git a/d.sql b/d.sql\n"
        "--- a/d.sql\n"
        "+++ b/d.sql\n"
        "@@ -5,2 +4,0 @@\n"
        "-gone one\n"
        "-gone two\n"
    )
    changed = parse_unified_diff(text)
    assert changed.get("d.sql", frozenset()) == frozenset()


def test_parse_rename_uses_new_path() -> None:
    text = (
        "diff --git a/old.sql b/new.sql\n"
        "similarity index 90%\n"
        "rename from old.sql\n"
        "rename to new.sql\n"
        "--- a/old.sql\n"
        "+++ b/new.sql\n"
        "@@ -1,0 +2,1 @@\n"
        "+added\n"
    )
    changed = parse_unified_diff(text)
    assert "new.sql" in changed
    assert changed["new.sql"] == frozenset({2})
    assert "old.sql" not in changed


def test_parse_multiple_files_and_hunks() -> None:
    text = (
        "diff --git a/one.sql b/one.sql\n"
        "--- a/one.sql\n"
        "+++ b/one.sql\n"
        "@@ -1 +1 @@\n"
        "+a\n"
        "@@ -10,0 +11,2 @@\n"
        "+b\n"
        "+c\n"
        "diff --git a/two.sql b/two.sql\n"
        "--- a/two.sql\n"
        "+++ b/two.sql\n"
        "@@ -1,0 +5,1 @@\n"
        "+d\n"
    )
    changed = parse_unified_diff(text)
    assert changed["one.sql"] == frozenset({1, 11, 12})
    assert changed["two.sql"] == frozenset({5})


# --- filter_to_changed_lines -------------------------------------------------


def test_declaration_findings_always_survive() -> None:
    decl = CheckFinding(
        kind=CheckFindingKind.CONTRACT_ISSUE,
        message="x",
        model_unique_id="model.p.m",
        column="c",
        contract="C",
        file_path="models/a.sql",
    )
    changed: ChangedLines = {"models/other.sql": frozenset({1})}
    assert filter_to_changed_lines((decl,), changed) == (decl,)


def test_finding_in_untouched_file_dropped() -> None:
    f = _structural("models/a.sql", line_start=3, line_end=3)
    changed: ChangedLines = {"models/b.sql": frozenset({3})}
    assert filter_to_changed_lines((f,), changed) == ()


def test_finding_on_changed_line_survives() -> None:
    f = _structural("models/a.sql", line_start=4, line_end=5)
    changed: ChangedLines = {"models/a.sql": frozenset({5, 6})}
    assert filter_to_changed_lines((f,), changed) == (f,)


def test_finding_on_unchanged_line_within_diff_extent_dropped() -> None:
    # The file was touched, the span has real line provenance, and it sits inside
    # the diffed extent (lines 4..20) yet hits none of the changed lines. The
    # provenance lines up, so the miss is real: drop it.
    f = _structural("models/a.sql", line_start=8, line_end=9)
    changed: ChangedLines = {"models/a.sql": frozenset({4, 5, 20})}
    assert filter_to_changed_lines((f,), changed) == ()


def test_finding_outside_diff_extent_kept_as_file_membership() -> None:
    # Compiled-vs-source skew: the finding's compiled span (lines 80..81) lies
    # beyond the source file's diffed extent (4..20), so line provenance is
    # ambiguous. Fall back to file membership and keep it rather than drop a
    # possibly real finding.
    f = _structural("models/a.sql", line_start=80, line_end=81)
    changed: ChangedLines = {"models/a.sql": frozenset({4, 5, 20})}
    assert filter_to_changed_lines((f,), changed) == (f,)


def test_unknown_line_span_kept_when_file_touched() -> None:
    # line_start == 0 means "model scope, line unknown"; a touched file keeps it.
    f = _structural("models/a.sql", line_start=0, line_end=0)
    changed: ChangedLines = {"models/a.sql": frozenset({4})}
    assert filter_to_changed_lines((f,), changed) == (f,)


def test_finding_with_no_file_path_dropped() -> None:
    # A structural finding with no source file cannot be located against any diff,
    # so under a --diff filter it has no claim to a changed line.
    f = _structural(None, line_start=4, line_end=4)
    changed: ChangedLines = {"models/a.sql": frozenset({4})}
    assert filter_to_changed_lines((f,), changed) == ()


def test_touched_file_with_only_deletions_drops_located_findings() -> None:
    # A file whose only change is deletions has an empty changed-line set: there is
    # no post-image line a finding can land on, so located findings drop. An
    # unknown-span finding (line 0) also drops because there is no changed line to
    # anchor file membership to.
    located = _structural("models/a.sql", line_start=4, line_end=4)
    unknown = _structural("models/a.sql", line_start=0, line_end=0)
    changed: ChangedLines = {"models/a.sql": frozenset()}
    assert filter_to_changed_lines((located, unknown), changed) == ()
