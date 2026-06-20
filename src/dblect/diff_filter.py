"""Limit structural findings to the lines a pull request touches.

On a PR, the findings worth a reviewer's attention are the ones on lines the PR
changed. ``dblect check --diff <base-ref>`` computes the per-file changed-line set
from ``git diff --unified=0 <base-ref>...HEAD`` and keeps only the structural
findings that land on a touched source file (and, where line provenance is
trustworthy, a touched line). This is the same idea code-coverage and lint tools use
to scope their output to a diff.

The compiled-vs-source line subtlety
------------------------------------

A structural :class:`~dblect.sql.patterns.Finding` carries ``line_start`` /
``line_end`` that index the model's *compiled* SQL (``compiled_code``), which dbt
renders with refs and macros expanded inline. A git diff, by contrast, hunks the
on-disk *source* file. For a ref-only model the two line up (``{{ ref(...) }}``
expands to one relation on the same line), but a macro that emits several lines from
one source line shifts every line below it, so a naive intersection of compiled-line
spans against source hunks would mislocate findings in macro-heavy models.

The honest contract this filter enforces keeps every real finding and uses line
numbers only where they can be trusted:

* A finding whose model source file has no changed line is dropped: the PR did not
  touch that model's file at all.
* Within a touched file, a finding is kept when its compiled span intersects the
  file's changed lines. When the span misses every changed line, it is kept only if
  the span lies *outside* the file's diffed extent (the lowest..highest changed
  line). A span beyond that extent signals compiled-vs-source skew, where the line
  numbers cannot be trusted, so we fall back to file-level membership rather than
  drop a finding that may be real. A span inside the diffed extent that still misses
  every changed line is a trustworthy miss, so it is dropped.
* A finding with no line span (``line_start == 0``, "model scope, line unknown") is
  kept whenever its file has any changed line, since there is no span to test.
* A finding with no source file (``file_path is None``) is dropped under ``--diff``:
  it cannot be located against any hunk.

A precise compiled-line to source-line back-map (so macro-heavy models filter at
line granularity too) is tracked separately as issue #124. This filter stays at
file-level plus best-effort line intersection.

Declaration-level findings (:class:`~dblect.check.findings.CheckFinding`) carry no
line number, so ``--diff`` leaves them untouched.

Falling back honestly
----------------------

When the working directory is not a git checkout, or the base ref does not resolve,
:func:`changed_lines_from_git` returns ``None`` and the caller renders the full
report. ``--diff`` narrows output when it can and degrades to the unfiltered report
when it cannot, never crashing the run over a missing ``.git`` or a bad ref.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import assert_never

from dblect.analysis import AnalysisFinding
from dblect.audit.walker import LocatedFinding
from dblect.check.findings import CheckFinding

# A source file path (relative, as dbt stores ``original_file_path`` and as git
# prints it) to the set of 1-indexed line numbers the diff added or changed on the
# post-image (new) side. A file present with an empty set was touched only by
# deletions, so it has no reachable changed line.
ChangedLines = Mapping[str, frozenset[int]]

# `@@ -<old> +<new> @@`, where each side is `start` or `start,count`. We read only
# the new side: the post-image is where surviving findings live.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
# `+++ b/path` names the post-image file. Renames and adds carry the new path here.
_NEW_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")


def parse_unified_diff(text: str) -> ChangedLines:
    """Parse ``git diff --unified=0`` output into a per-file changed-line map.

    Reads each file's ``+++`` header for the post-image path and each ``@@`` hunk
    for the new-side line range, collecting the 1-indexed lines the hunk added. A
    deletion-only hunk (new count ``0``) contributes no lines, so a file changed
    only by deletions maps to an empty set. ``/dev/null`` post-images (a full file
    deletion) are skipped, since there is no surviving file to attribute findings to.
    """
    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in text.splitlines():
        new_file = _NEW_FILE_RE.match(line)
        if new_file is not None:
            path: str = new_file.group(1).strip()
            if path == "/dev/null":
                current = None
                continue
            current = path
            changed.setdefault(path, set())
            continue
        hunk = _HUNK_RE.match(line)
        if hunk is not None and current is not None:
            start = int(hunk.group(1))
            count = 1 if hunk.group(2) is None else int(hunk.group(2))
            changed[current].update(range(start, start + count))
    return {path: frozenset(lines) for path, lines in changed.items()}


def filter_to_changed_lines(
    findings: Sequence[AnalysisFinding], changed: ChangedLines
) -> tuple[AnalysisFinding, ...]:
    """Keep the findings a ``--diff`` run should report, per the module contract.

    Declaration findings pass through unchanged. Structural findings are kept by
    file membership and best-effort line intersection. Pure over its inputs, so the
    contract is exercised without invoking git.
    """
    return tuple(f for f in findings if _survives(f, changed))


def filter_located_to_changed_lines(
    findings: Sequence[LocatedFinding], changed: ChangedLines
) -> tuple[LocatedFinding, ...]:
    """Keep the structural findings a ``--diff`` run should report.

    The structural-only counterpart to :func:`filter_to_changed_lines`, so a caller
    holding the audit family alone keeps the narrower ``LocatedFinding`` element type
    rather than widening to the sealed union.
    """
    return tuple(f for f in findings if _located_survives(f, changed))


def _survives(finding: AnalysisFinding, changed: ChangedLines) -> bool:
    match finding:
        case CheckFinding():
            return True
        case LocatedFinding():
            return _located_survives(finding, changed)
    assert_never(finding)


def _located_survives(finding: LocatedFinding, changed: ChangedLines) -> bool:
    if finding.file_path is None:
        return False
    touched = changed.get(finding.file_path)
    if not touched:
        # File untouched, or touched only by deletions: no post-image line to land
        # on, so nothing the diff added carries this finding.
        return False
    span = finding.finding.line_start, finding.finding.line_end
    if span[0] == 0:
        # Line unknown (model scope): a touched file is the only signal we have.
        return True
    lo, hi = span
    if any(line in touched for line in range(lo, hi + 1)):
        return True
    # The span misses every changed line. Trust the miss only when the span sits
    # inside the file's diffed extent; a span beyond it is compiled-vs-source skew,
    # where we fall back to file membership rather than drop a possibly real finding.
    extent_lo, extent_hi = min(touched), max(touched)
    within_extent = lo >= extent_lo and hi <= extent_hi
    return not within_extent


def changed_lines_from_git(project_dir: Path, base_ref: str) -> ChangedLines | None:
    """The per-file changed-line map for ``base_ref...HEAD``, or ``None`` to fall back.

    Runs ``git diff --unified=0 <base-ref>...HEAD`` rooted at ``project_dir`` so the
    paths it returns are relative to the repository, matching dbt's
    ``original_file_path``. Returns ``None`` when the directory is not a git checkout
    or the ref does not resolve, so the caller renders the full report instead of
    crashing. The merge-base form (``...``) scopes the diff to what HEAD changed
    since it forked from the base, which is what a PR review wants.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(project_dir),
                "diff",
                "--unified=0",
                "--no-color",
                f"{base_ref}...HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    return parse_unified_diff(completed.stdout)
