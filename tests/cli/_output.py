"""Helpers for asserting against ``dblect`` CLI output in tests.

Typer/Rich renders ``BadParameter`` inside a Panel and highlights CLI flags like
``--dialect`` or ``--base-manifest`` with colour escapes when a colour-capable
terminal is detected (which CI runners report). That highlighting splits the literal
flag across ANSI sequences, so substring checks must strip the escapes first.
"""

from __future__ import annotations

import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(text: str) -> str:
    """Return ``text`` with ANSI colour/formatting escapes removed."""
    return _ANSI_RE.sub("", text)
