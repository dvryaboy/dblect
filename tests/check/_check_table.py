"""The shared check-table runner.

A detector's declaration-level tests are often the same shape: one model's SQL
run through ``run_check``, checked against the finding kinds it should report
(in order) and, when there is exactly one, a wording fragment its message
should carry. ``CheckCase`` names a table row of that shape; ``run_check_case``
checks one. An empty ``expected`` is a silent row, and a silent row still
asserts the model actually built, so "no findings" is never confused with
"nothing was analyzed".
"""

from __future__ import annotations

from dataclasses import dataclass

from dblect.adapters import AdapterProfile
from dblect.check import CheckFindingKind, run_check
from dblect.manifest import Manifest


@dataclass(frozen=True, slots=True)
class CheckCase:
    id: str
    sql: str
    expected: tuple[CheckFindingKind, ...] = ()
    """The finding kinds ``run_check`` should report, in order. Empty is silent."""
    wording: tuple[str, ...] = ()
    """Fragments the sole finding's message must contain; only checked when
    ``expected`` names exactly one kind."""
    absent: tuple[str, ...] = ()
    """Fragments the sole finding's message must not contain (a remediation that
    must never prescribe the wrong fix, a verdict that must never say "violated");
    checked under the same one-finding rule as ``wording``."""


def run_check_case(case: CheckCase, manifest: Manifest, profile: AdapterProfile) -> None:
    report = run_check(manifest, profile)
    kinds = tuple(f.kind for f in report.findings)
    assert kinds == case.expected, f"{case.id}: got {kinds}, expected {case.expected}"
    if case.wording or case.absent:
        assert len(report.findings) == 1, f"{case.id}: wording needs exactly one finding"
        message = report.findings[0].message
        for fragment in case.wording:
            assert fragment in message, f"{case.id}: {fragment!r} missing from {message!r}"
        for fragment in case.absent:
            assert fragment not in message, f"{case.id}: {fragment!r} present in {message!r}"
    if not case.expected:
        assert report.models_analyzed > 0, f"{case.id}: silent row's model never built"
        assert report.unbuilt == (), (
            f"{case.id}: silent row's model failed to build: {report.unbuilt}"
        )
