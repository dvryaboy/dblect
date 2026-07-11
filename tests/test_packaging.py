"""The install-boundary contract: every third-party module the shipped package
imports is a declared runtime dependency.

This is the class of bug a passing test suite hides, because the dev environment
has every dependency installed (through the dev group and dbt's transitive tree),
so an import that is missing from ``[project.dependencies]`` still resolves under
test and only breaks on a clean ``pip install dblect``. Here we read the declared
runtime requirements from the installed metadata and the actual imports from the
source AST, and assert the former covers the latter, so a fresh install runs.
"""

from __future__ import annotations

import ast
import sys
from importlib.metadata import PackageNotFoundError, packages_distributions, requires
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_SRC = Path(__file__).parent.parent / "src" / "dblect"


def _runtime_distributions() -> set[str]:
    """Canonical names of dblect's runtime dependencies (extras excluded).

    ``requires`` returns every requirement string, including optional-dependency
    entries carrying an ``extra == '...'`` marker. A runtime dependency is one
    whose marker does not gate on an extra, so it is installed by a bare
    ``pip install dblect``."""
    reqs = requires("dblect")
    assert reqs is not None, "dblect metadata carries no requirements"
    runtime: set[str] = set()
    for raw in reqs:
        req = Requirement(raw)
        marker = str(req.marker) if req.marker else ""
        if "extra ==" in marker:
            continue
        runtime.add(canonicalize_name(req.name))
    return runtime


def _third_party_imports() -> set[str]:
    """Top-level import names in the shipped source that are neither stdlib nor
    first-party. Relative imports carry no top-level name and are skipped."""
    stdlib = sys.stdlib_module_names
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import (no distribution behind it).
                roots = [node.module.split(".")[0]] if node.level == 0 and node.module else []
            else:
                continue
            for root in roots:
                if root != "dblect" and root not in stdlib:
                    found.add(root)
    return found


def test_every_runtime_import_is_a_declared_dependency() -> None:
    runtime = _runtime_distributions()
    import_to_dists = packages_distributions()
    undeclared: dict[str, str] = {}
    for module in sorted(_third_party_imports()):
        dists = import_to_dists.get(module)
        if not dists:
            # Not provided by any installed distribution: either a namespace the
            # mapping cannot see or a genuinely missing package. Surface it.
            undeclared[module] = "no installed distribution provides this import"
            continue
        if not any(canonicalize_name(d) in runtime for d in dists):
            undeclared[module] = f"provided by {dists}, none declared as a runtime dependency"
    assert not undeclared, (
        "shipped source imports modules absent from [project.dependencies]; a clean "
        f"`pip install dblect` would fail on them: {undeclared}"
    )


def test_dblect_metadata_is_installed() -> None:
    # The audit above is only meaningful against installed metadata; guard the
    # precondition so a missing install fails loudly rather than skipping silently.
    try:
        assert _runtime_distributions()
    except PackageNotFoundError:  # pragma: no cover - defensive
        pytest.fail("dblect is not installed; run the suite via `uv run pytest`")
