"""The sqlglot-build compatibility guard.

dblect defines synthetic AST nodes (``UnionConfluence``, ``OuterJoinNull``) and a
caching schema by subclassing sqlglot's ``Expression`` / ``MappingSchema``. sqlglot's
compiled build (``sqlglotc``, pulled by ``sqlglot[c]``) is mypyc-compiled, and mypyc
forbids an interpreted class from *instantiating* a subclass of a compiled one. So a
compiled sqlglot present in the environment (installed deliberately, or dragged in by
a co-resident tool) makes analysis raise ``TypeError: interpreted classes cannot
inherit from compiled`` deep in the build, or degrade to missing findings.

The contract pinned here: analysis fails fast at the door with an actionable error
naming the culprit and its remedy, rather than a cryptic mid-run traceback or silent
under-reporting.
"""

from __future__ import annotations

import pytest

from dblect.adapters import profile_for_adapter
from dblect.analysis import analyze
from dblect.manifest import Manifest, Node, ResourceType
from dblect.sql import compat
from dblect.sql.compat import (
    SqlglotCompatibilityError,
    ensure_pure_sqlglot,
    sqlglot_supports_subclassing,
)

_DUCKDB = profile_for_adapter("duckdb")


def _manifest(sql: str = "SELECT 1 AS a") -> Manifest:
    node = Node(
        unique_id="model.pkg.m",
        name="m",
        resource_type=ResourceType.MODEL,
        fqn=("pkg", "m"),
        package_name="pkg",
        schema=None,
        raw_code=sql,
        compiled_code=sql,
        original_file_path="models/m.sql",
        columns={},
    )
    return Manifest(schema_version="x", adapter_type="duckdb", nodes={node.unique_id: node})


def _compiled_sqlglot_active() -> bool:
    # sqlglotc overlays compiled `.so` modules into the `sqlglot` package (its top-level
    # is `sqlglot`, so `import sqlglotc` is not a reliable signal). A compiled module
    # loaded in preference to its `.py` source is the observable marker.
    import sqlglot.parser

    return sqlglot.parser.__file__.endswith((".so", ".pyd"))


def test_detection_true_on_pure_sqlglot() -> None:
    # The test environment installs pure-Python sqlglot, whose classes an interpreted
    # subclass can instantiate. This also proves the probe does not false-positive by
    # relying on some other constructor requirement.
    assert sqlglot_supports_subclassing() is True


def test_guard_is_silent_on_pure_sqlglot() -> None:
    # Returns None, raises nothing: the common path stays a no-op.
    assert ensure_pure_sqlglot() is None


def test_guard_raises_actionable_error_when_subclassing_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compat, "sqlglot_supports_subclassing", lambda: False)
    with pytest.raises(SqlglotCompatibilityError) as excinfo:
        ensure_pure_sqlglot()
    message = str(excinfo.value)
    # Names the culprit and both spellings a user would search for, and the remedy.
    assert "sqlglotc" in message
    assert "sqlglot[c]" in message
    assert "uninstall" in message.lower()


@pytest.mark.skipif(
    not _compiled_sqlglot_active(),
    reason="requires sqlglot's compiled build (sqlglotc) to be active",
)
def test_real_compiled_build_is_detected_and_refused() -> None:
    # End-to-end against the actual compiled build, not a monkeypatched detector: the
    # probe reports the incompatibility and the guard raises. Skipped in the pure-Python
    # environment CI runs in; exercised wherever the compiled build is installed.
    assert sqlglot_supports_subclassing() is False
    with pytest.raises(SqlglotCompatibilityError):
        ensure_pure_sqlglot()


def test_analyze_fails_fast_through_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # The single analysis door runs the guard before any build, so a compiled sqlglot
    # surfaces as this clear error rather than a TypeError from the lineage builder.
    monkeypatch.setattr(compat, "sqlglot_supports_subclassing", lambda: False)
    with pytest.raises(SqlglotCompatibilityError):
        analyze(_manifest(), _DUCKDB)
