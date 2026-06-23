"""Keep the bootstrap skill's DSL honest against the live API.

The skill ships its declaration examples inline, so an example can rot silently
when the surface it teaches moves. Every ``python`` block in ``skill.md`` is meant
to be real, runnable-against-the-API code; this execs each one against the actual
imports so a renamed symbol, a moved import, or a dropped method fails CI here
instead of misleading an agent that copies from the skill.

Partial or illustrative snippets belong in ``text`` blocks, not ``python`` ones.
"""

from __future__ import annotations

import pytest

from dblect.bootstrap import python_examples
from dblect.types import isolated_registry


def test_skill_has_executable_examples() -> None:
    # A vacuous guard (zero blocks) would pass while teaching nothing; pin a floor.
    assert len(python_examples()) >= 3


@pytest.mark.parametrize("block", python_examples(), ids=lambda b: b.splitlines()[0][:50])
def test_skill_python_block_runs_against_live_api(block: str) -> None:
    # Each block carries its own imports and defines its own types/contracts. An
    # isolated registry keeps contract classes from colliding across blocks or
    # leaking into other tests' registries.
    with isolated_registry():
        namespace: dict[str, object] = {}
        # dont_inherit keeps this test module's `from __future__ import annotations`
        # from stringizing the block's annotations, which would defer their evaluation
        # past the class body where the block's own imports are in scope.
        exec(compile(block, "skill.md", "exec", dont_inherit=True), namespace)
