"""``dblect setup <target>`` installs the bootstrap skill into a project.

The skill content is pinned in ``tests/bootstrap``; here we confirm the installer
wiring: each target writes to its surface's path with the right wrapper, ``--print``
writes nothing, the ``AGENTS.md`` block is idempotent and preserves the user's
prose, and an unknown target is rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dblect.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_setup_claude_writes_skill_file(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "claude", str(tmp_path)])
    assert result.exit_code == 0, result.output

    skill = tmp_path / ".claude" / "skills" / "dblect-bootstrap" / "SKILL.md"
    assert skill.exists()
    body = skill.read_text()
    assert body.startswith("---\n")
    assert "name: dblect-bootstrap" in body
    assert "ModelContract" in body  # the body, not just the frontmatter
    assert str(skill) in result.output


def test_setup_cursor_writes_rule_file(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "cursor", str(tmp_path)])
    assert result.exit_code == 0, result.output

    rule = tmp_path / ".cursor" / "rules" / "dblect-bootstrap.mdc"
    assert rule.exists()
    body = rule.read_text()
    assert body.startswith("---\n")
    assert "alwaysApply" in body
    assert "ModelContract" in body


def test_setup_codex_appends_delimited_block(runner: CliRunner, tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# House rules\n\nBe nice.\n")

    result = runner.invoke(app, ["setup", "codex", str(tmp_path)])
    assert result.exit_code == 0, result.output

    body = agents.read_text()
    assert "Be nice." in body  # user's prose survives
    assert body.count("BEGIN dblect:bootstrap") == 1
    assert "ModelContract" in body


def test_setup_codex_is_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    # Re-running replaces dblect's own block rather than stacking a second copy.
    runner.invoke(app, ["setup", "codex", str(tmp_path)])
    runner.invoke(app, ["setup", "codex", str(tmp_path)])

    body = (tmp_path / "AGENTS.md").read_text()
    assert body.count("BEGIN dblect:bootstrap") == 1
    assert body.count("END dblect:bootstrap") == 1


def test_setup_print_writes_nothing(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "claude", str(tmp_path), "--print"])
    assert result.exit_code == 0, result.output
    assert "ModelContract" in result.output
    assert not (tmp_path / ".claude").exists()


def test_setup_rejects_unknown_target(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "emacs", str(tmp_path)])
    assert result.exit_code != 0
