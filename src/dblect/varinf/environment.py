"""A jinja2 environment that parses dbt source templates without rendering them.

A bare ``jinja2.Environment`` rejects the tags dbt relies on. A probe of the
fixture's macro bodies found the failures fall into a small, closed set: two are
standard Jinja extensions dbt enables (``do`` and the loop controls), and the
rest are dbt block tags that share one shape. We enable the two extensions and
add one generic block-tag extension, so the var walker parses the same source
dbt does.

The block-tag extension parses the body *into* statements rather than skipping
to the end tag, so a ``var()`` inside a snapshot (including one nested in an
``{% if %}``) keeps its syntactic context and is classified correctly. Only the
header tokens (``snapshot name``, ``materialization ..., adapter='x'``) are
skipped, where vars do not live.
"""

from __future__ import annotations

from functools import cache
from typing import ClassVar

from jinja2 import Environment, nodes
from jinja2.ext import Extension
from jinja2.parser import Parser

# dbt's block tags share the shape ``{% TAG ...header... %} body {% endTAG %}``.
# The set is dbt's documented vocabulary, so it is closed; extending it is a
# one-line change here, and an un-enumerated tag costs coverage (an opaque
# diagnostic), never correctness.
_DBT_BLOCK_TAGS = frozenset({"materialization", "snapshot", "docs", "test"})


class DbtBlockTags(Extension):
    """Parse dbt block tags by skipping the header and parsing the body as statements."""

    # jinja2 declares ``tags`` as a plain class attribute; the ClassVar annotation
    # keeps ruff happy about the mutable default and pyright treats it as the same
    # class-level slot the base uses.
    tags: ClassVar[set[str]] = set(_DBT_BLOCK_TAGS)  # pyright: ignore[reportIncompatibleVariableOverride]

    def parse(self, parser: Parser) -> nodes.Node:
        stream = parser.stream
        tag = stream.current.value
        lineno = next(stream).lineno
        # Skip the header tokens (everything up to the block end); vars do not
        # appear in a snapshot name or a materialization's adapter kwarg.
        while stream.current.type != "block_end":
            next(stream)
        body = parser.parse_statements((f"name:end{tag}",), drop_needle=True)
        return nodes.Scope(body, lineno=lineno)


def make_environment() -> Environment:
    """Build the environment the var walker parses source Jinja with.

    Enables the two stdlib extensions dbt uses and the dbt block-tag extension.
    The environment never renders; it is used only for ``parse``.
    """
    return Environment(
        extensions=[
            "jinja2.ext.do",
            "jinja2.ext.loopcontrols",
            DbtBlockTags,
        ],
        # Source is parsed, never rendered, so autoescape is irrelevant; keep it
        # off so no behavior depends on it.
        autoescape=False,
    )


@cache
def shared_environment() -> Environment:
    """The environment the walker parses with, materialized once on first use.

    Building an environment wires three extensions, and the walker parses one
    environment per node otherwise. The environment only ever ``parse``s (it never
    renders and holds no per-source state), so a single instance serves every walk.
    Callers that want an isolated environment build one with :func:`make_environment`.
    """
    return make_environment()
