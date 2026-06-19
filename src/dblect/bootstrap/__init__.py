"""The bootstrap skill: instructions that drive an AI coding agent through
investigating a dbt project and drafting dblect's semantic declaration layer.

The authored content lives in ``skill.md`` (harness-agnostic markdown). ``dblect
setup <target>`` adapts it to a Claude Code skill, a Cursor rule, or an
``AGENTS.md`` block. None of this is on the analysis path: it is instruction text
the agent reads, versioned beside the DSL it teaches. ``skill.md`` keeps its
declaration examples honest by being runnable code, pinned by ``tests/bootstrap``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from importlib.resources import files
from pathlib import Path

SKILL_NAME = "dblect-bootstrap"
SKILL_DESCRIPTION = (
    "Investigate a dbt project and draft dblect domain types and contracts on the "
    "columns whose meaning matters, then run `dblect check` and self-correct."
)

# AGENTS.md owns the user's prose; dblect writes only between these markers so a
# re-run replaces its own block instead of stacking a second copy.
_BEGIN = "<!-- BEGIN dblect:bootstrap -->"
_END = "<!-- END dblect:bootstrap -->"
_CODEX_BLOCK = re.compile(re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\n?", re.DOTALL)

_PYTHON_BLOCK = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)


class SetupTarget(StrEnum):
    """An AI coding agent surface ``dblect setup`` can install the skill into."""

    CLAUDE = "claude"
    CURSOR = "cursor"
    CODEX = "codex"


def skill_body() -> str:
    """The authored, harness-agnostic skill markdown."""
    return (files("dblect.bootstrap") / "skill.md").read_text(encoding="utf-8")


def python_examples(body: str | None = None) -> list[str]:
    """Every fenced ``python`` block in the skill. Each is self-contained, runnable
    code, so the drift guard can exec them against the live API."""
    return [m.group(1) for m in _PYTHON_BLOCK.finditer(body if body is not None else skill_body())]


def render(target: SetupTarget) -> str:
    """The skill wrapped for ``target``: front matter for the file surfaces, a
    delimited block for ``AGENTS.md``."""
    body = skill_body()
    if target is SetupTarget.CLAUDE:
        return f"---\nname: {SKILL_NAME}\ndescription: {SKILL_DESCRIPTION}\n---\n\n{body}"
    if target is SetupTarget.CURSOR:
        return f"---\ndescription: {SKILL_DESCRIPTION}\nglobs:\nalwaysApply: false\n---\n\n{body}"
    return f"{_BEGIN}\n{body.rstrip()}\n{_END}\n"


def target_path(target: SetupTarget, project_dir: Path) -> Path:
    """Where ``target`` reads its skill from inside ``project_dir``."""
    if target is SetupTarget.CLAUDE:
        return project_dir / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
    if target is SetupTarget.CURSOR:
        return project_dir / ".cursor" / "rules" / f"{SKILL_NAME}.mdc"
    return project_dir / "AGENTS.md"


def install(target: SetupTarget, project_dir: Path) -> Path:
    """Write the skill into ``project_dir`` for ``target`` and return the path. The
    file surfaces own their path, so writing is a replace; ``AGENTS.md`` is shared,
    so dblect's block is merged in, preserving the rest of the file."""
    path = target_path(target, project_dir)
    document = render(target)
    if target is SetupTarget.CODEX:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(_merge_codex_block(existing, document), encoding="utf-8")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path


def _merge_codex_block(existing: str, block: str) -> str:
    if _CODEX_BLOCK.search(existing):
        return _CODEX_BLOCK.sub(lambda _: block, existing)
    separator = (
        ""
        if not existing or existing.endswith("\n\n")
        else "\n"
        if existing.endswith("\n")
        else "\n\n"
    )
    return f"{existing}{separator}{block}"
