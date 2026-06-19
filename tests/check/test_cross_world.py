"""The cross-world differencing for the incremental check, pinned without dbt.

``cross_world_findings`` keeps a finding that holds in a strict subset of the
analyzed worlds and drops one that holds in every world. The differencing keys on
a stable identity, so a finding whose message or line span differs between the two
compiled worlds (which they do, since the SQL differs) is still recognized as the
same issue and is not mistaken for a world-varying one.
"""

from __future__ import annotations

from dblect.analysis import AnalysisFinding
from dblect.audit import LocatedFinding
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.check.incremental import cross_world_findings
from dblect.execution.incremental import FULL_REFRESH_WORLD, STEADY_STATE_WORLD
from dblect.sql import Finding, FindingKind

_MODEL = "model.p.inc"


def _located(message: str, *, snippet: str, line_start: int) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id=_MODEL,
        file_path="models/inc.sql",
        finding=Finding(
            kind=FindingKind.JOIN_FANOUT,
            message=message,
            sql_snippet=snippet,
            line_start=line_start,
            line_end=line_start,
        ),
    )


def _contradiction(message: str) -> CheckFinding:
    return CheckFinding(
        kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
        message=message,
        model_unique_id=_MODEL,
        column="amount",
    )


def test_finding_in_one_world_only_is_cross_world() -> None:
    fanout = _located(
        "join to state can multiply rows", snippet="JOIN state ON e.id = s.id", line_start=9
    )
    per_world: dict[object, list[AnalysisFinding]] = {
        FULL_REFRESH_WORLD: [],
        STEADY_STATE_WORLD: [fanout],
    }

    (varying,) = cross_world_findings(per_world)  # type: ignore[arg-type]

    assert varying.worlds == frozenset({STEADY_STATE_WORLD})
    assert varying.representative == fanout


def test_finding_in_every_world_is_world_invariant() -> None:
    shared = _contradiction("declared usd contradicted")
    per_world: dict[object, list[AnalysisFinding]] = {
        FULL_REFRESH_WORLD: [shared],
        STEADY_STATE_WORLD: [shared],
    }

    assert cross_world_findings(per_world) == ()  # type: ignore[arg-type]


def test_message_and_line_drift_do_not_create_a_false_flip() -> None:
    # The same structural finding renders with a different message and line span in
    # each world (the surrounding SQL differs), yet it is one issue present in both
    # worlds. Keying the diff on identity rather than whole-finding equality keeps it
    # world-invariant; a naive equality diff would report it twice as world-varying.
    full = _located(
        "fanout (full-refresh wording)", snippet="JOIN state ON e.id = s.id", line_start=4
    )
    steady = _located(
        "fanout (steady-state wording)", snippet="JOIN state ON e.id = s.id", line_start=11
    )
    contradiction_full = _contradiction("declared usd contradicted by usd-eur")
    contradiction_steady = _contradiction("declared usd contradicted by gbp")
    per_world: dict[object, list[AnalysisFinding]] = {
        FULL_REFRESH_WORLD: [full, contradiction_full],
        STEADY_STATE_WORLD: [steady, contradiction_steady],
    }

    assert cross_world_findings(per_world) == ()  # type: ignore[arg-type]
