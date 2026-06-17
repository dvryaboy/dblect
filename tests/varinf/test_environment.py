"""The parsing environment must accept dbt's tag vocabulary and parse block-tag
bodies *into* statements, so a ``var()`` inside a snapshot keeps its context.

The load-bearing test is that every macro body in the fixture manifest parses:
a dbt tag we have not enumerated then surfaces as a failing test here rather than
as a silent opaque-ing at analysis time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jinja2 import nodes

from dblect.varinf.environment import make_environment


@pytest.fixture(scope="module")
def jaffle_raw_macros(jaffle_manifest_path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(jaffle_manifest_path.read_text())
    return raw["macros"]


def test_every_fixture_macro_body_parses(jaffle_raw_macros: dict[str, Any]) -> None:
    env = make_environment()
    failures: list[str] = []
    for uid, macro in jaffle_raw_macros.items():
        try:
            env.parse(macro["macro_sql"])
        except Exception as exc:  # the point is to catch any parse failure at all
            failures.append(f"{uid}: {type(exc).__name__}: {exc}")
    assert not failures, "macro bodies failed to parse:\n" + "\n".join(failures)


def test_do_extension_enabled() -> None:
    # `{% do ... %}` is a stdlib extension dbt relies on; a bare Environment rejects it.
    env = make_environment()
    env.parse("{% do [].append(1) %}")


def test_loop_controls_enabled() -> None:
    env = make_environment()
    env.parse("{% for x in items %}{% if x %}{% continue %}{% endif %}{% endfor %}")


def test_snapshot_body_parses_into_statements() -> None:
    # The block-tag body must parse as real statements (a Scope), not be skipped,
    # so a var() inside survives with its syntactic context.
    env = make_environment()
    template = (
        "{% snapshot s %}{% if var('full_refresh') %}a{% else %}b{% endif %}{% endsnapshot %}"
    )
    ast = env.parse(template)
    # An If node lives somewhere in the parsed tree: the body was parsed, not skipped.
    assert any(isinstance(n, nodes.If) for n in ast.find_all(nodes.If))


@pytest.mark.parametrize("tag", ["materialization", "snapshot", "docs", "test"])
def test_dbt_block_tags_accepted(tag: str) -> None:
    env = make_environment()
    # Header tokens after the tag name are skipped; the body parses.
    env.parse(f"{{% {tag} foo, adapter='x' %}}body{{% end{tag} %}}")
