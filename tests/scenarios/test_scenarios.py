"""The demo scenarios, each run end to end through ``dblect check``.

Every case under ``tests/fixtures/scenarios/cases`` is a small, realistic change a
developer makes to a currency-aware jaffle: a stale type, a new report that mixes
currencies, a sound per-order rollup. Each ships a committed manifest (compiled by
``scripts/refresh_scenarios.sh``), a ``dblect/`` declaration package, and an
``expected.yml`` listing the findings dblect should produce. This test loads the
declarations, runs the check against the manifest, and asserts the findings match.
No dbt is needed at run time; the manifests are committed.

These are the user-facing counterpart to the substrate tests in ``tests/check``:
they confirm the same findings fire (and, for the sound case, do not fire) on real
dbt-compiled SQL driven by real ``ModelContract`` declarations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from dblect.check import CheckFinding, run_check
from dblect.loader import load_declarations
from dblect.manifest import Manifest

_CASES_DIR = Path(__file__).parent.parent / "fixtures" / "scenarios" / "cases"


def _cases() -> list[Path]:
    return sorted(p for p in _CASES_DIR.iterdir() if (p / "manifest.json").exists())


def _finding_key(finding: CheckFinding) -> tuple[str, str | None, str | None]:
    return (finding.kind.value, finding.model_unique_id, finding.column)


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _expected(case: Path) -> list[tuple[str, str | None, str | None]]:
    loaded: object = yaml.safe_load((case / "expected.yml").read_text())
    doc = cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else {}
    findings = cast("list[dict[str, Any]]", doc.get("findings") or [])
    return sorted(
        (str(f["kind"]), _opt_str(f.get("model")), _opt_str(f.get("column"))) for f in findings
    )


@pytest.mark.parametrize("case", _cases(), ids=lambda p: p.name)
def test_scenario_findings_match(case: Path) -> None:
    manifest = Manifest.from_file(case / "manifest.json")
    loaded = load_declarations(case)
    assert loaded.issues == (), f"declarations failed to load: {loaded.issues}"

    report = run_check(manifest, registry=loaded.registry, dialect="duckdb")
    actual = sorted(_finding_key(f) for f in report.findings)
    assert actual == _expected(case)


def test_every_case_is_discovered() -> None:
    # A committed manifest is what makes a case runnable; guard against a new case
    # whose manifest was never generated (refresh_scenarios.sh not run).
    all_dirs = {p.name for p in _CASES_DIR.iterdir() if p.is_dir()}
    runnable = {p.name for p in _cases()}
    assert all_dirs == runnable, f"missing manifest.json for: {sorted(all_dirs - runnable)}"
