"""End-to-end tests for ``dblect check --diff <base-ref>`` over a real git repo.

These drive the whole path: materialise the vendored jaffle project (manifest plus
its model source files reconstructed from each node's ``raw_code``) into a throwaway
git checkout, edit a model across a commit, and assert that ``--diff`` keeps only the
findings on touched lines while leaving declaration findings and skips alone. The
diff parsing and the intersection contract are pinned without git in
``tests/test_diff_filter.py``; this module pins the git integration and the
honest-fallback behaviour.

Jaffle is ref-only: ``{{ ref(...) }}`` expands to a single quoted relation on the
same line, so the compiled SQL and the on-disk source keep the same line numbers.
That alignment is what lets a source-file diff intersect a compiled-line span
honestly. The lone jaffle structural finding (``null_group_after_outer_join``) lands
on line 44 of ``models/customers.sql``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dblect.cli import app
from dblect.manifest import Manifest

_FINDING_FILE = "models/customers.sql"
_FINDING_LINE = 44
_FINDING_KIND = "null_group_after_outer_join"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _materialise_jaffle(project: Path, manifest_path: Path) -> None:
    """Write the jaffle manifest into ``project/target`` and reconstruct each
    model's source file from its ``raw_code`` at its ``original_file_path`` so the
    on-disk tree matches what the manifest and a git diff see."""
    (project / "target").mkdir(parents=True, exist_ok=True)
    (project / "dbt_project.yml").write_text("name: jaffle_shop\nprofile: jaffle_shop\n")
    shutil.copy(manifest_path, project / "target" / "manifest.json")
    manifest = Manifest.from_file(manifest_path)
    for node in manifest.models.values():
        if node.original_file_path is None or node.raw_code is None:
            continue
        dest = project / node.original_file_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(node.raw_code)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def repo(tmp_path: Path, jaffle_manifest_path: Path) -> Path:
    """A git checkout of the jaffle project committed on ``main``."""
    project = tmp_path / "proj"
    project.mkdir()
    _materialise_jaffle(project, jaffle_manifest_path)
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "t@t.test")
    _git(project, "config", "user.name", "t")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "base")
    return project


def _edit_line(repo: Path, rel: str, line_no: int, new_text: str) -> None:
    path = repo / rel
    lines = path.read_text().splitlines()
    lines[line_no - 1] = new_text
    path.write_text("\n".join(lines) + "\n")


def _check_kinds(runner: CliRunner, repo: Path, *extra: str) -> set[str]:
    result = runner.invoke(app, ["check", str(repo), "--format", "json", "--no-fail", *extra])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    return {f["kind"] for f in payload["findings"] if f["family"] == "structural"}


def test_baseline_without_diff_reports_the_hazard(runner: CliRunner, repo: Path) -> None:
    assert _FINDING_KIND in _check_kinds(runner, repo)


def test_change_on_the_hazard_line_keeps_it(runner: CliRunner, repo: Path) -> None:
    # Edit the very line the finding lands on; the touched line carries the hazard.
    _edit_line(repo, _FINDING_FILE, _FINDING_LINE, "    group by orders.customer_id  -- grp")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "annotate group-by")
    assert _FINDING_KIND in _check_kinds(runner, repo, "--diff", "main~1")


def test_added_line_above_hazard_shifts_and_keeps_via_intersection(
    runner: CliRunner, repo: Path
) -> None:
    # Insert a comment line right above the hazard. The diff records the inserted
    # line, and the hazard slides down onto it on the post-image side, so it stays.
    path = repo / _FINDING_FILE
    lines = path.read_text().splitlines()
    lines.insert(_FINDING_LINE - 1, "    -- group customers")
    path.write_text("\n".join(lines) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "comment above group-by")
    # The on-disk source no longer matches the committed manifest's compiled line,
    # but the inserted line is exactly where the hazard now sits, so file+line keep
    # it. (The manifest still pins the hazard at line 44, which is now the inserted
    # changed line.)
    assert _FINDING_KIND in _check_kinds(runner, repo, "--diff", "main~1")


def test_change_in_another_file_filters_the_untouched_hazard(runner: CliRunner, repo: Path) -> None:
    # Touch a staging model; customers.sql is untouched, so its hazard is filtered.
    _edit_line(repo, "models/staging/stg_customers.sql", 1, "-- staging customers")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "touch staging only")
    assert _FINDING_KIND not in _check_kinds(runner, repo, "--diff", "main~1")


def test_change_same_file_off_the_hazard_line_filters_it(runner: CliRunner, repo: Path) -> None:
    # Edit two lines that straddle the hazard line (the join above it and a column
    # below it) while leaving line 44 itself untouched. The file is touched and the
    # hazard's compiled span sits inside the diffed extent yet on none of the changed
    # lines, so the honest contract reads the miss as real and drops it.
    _edit_line(repo, _FINDING_FILE, 41, "    left join orders  -- joined")
    _edit_line(repo, _FINDING_FILE, 50, "    select  -- final projection")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "annotate around group-by")
    assert _FINDING_KIND not in _check_kinds(runner, repo, "--diff", "main~1")


def test_not_a_git_checkout_falls_back_to_full_output(
    runner: CliRunner, tmp_path: Path, jaffle_manifest_path: Path
) -> None:
    project = tmp_path / "nogit"
    project.mkdir()
    _materialise_jaffle(project, jaffle_manifest_path)
    result = runner.invoke(
        app, ["check", str(project), "--format", "json", "--no-fail", "--diff", "main"]
    )
    assert result.exit_code == 0, result.output
    kinds = {
        f["kind"] for f in json.loads(result.stdout)["findings"] if f["family"] == "structural"
    }
    assert _FINDING_KIND in kinds


def test_unresolvable_base_ref_falls_back_to_full_output(runner: CliRunner, repo: Path) -> None:
    assert _FINDING_KIND in _check_kinds(runner, repo, "--diff", "does-not-exist")
