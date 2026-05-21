"""Smoke tests: the package imports and reports a version."""

from __future__ import annotations

import re

import dblect


def test_version_is_semver_shaped() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", dblect.__version__)


def test_cli_module_imports() -> None:
    from dblect.cli import app

    assert app.info.name == "dblect"
