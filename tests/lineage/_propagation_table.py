"""The shared propagation-table runner.

A property's propagation tests are almost always the same shape: parse one SQL
string against a fixed set of declared facts, propagate, and check one output
column's value. ``PropagationCase`` names a table row of that shape, and
``run_propagation_case`` checks one against a property module's own ``run``
function (however that module builds its graph and grounding, since that part
is property-specific); the test file supplies the table and one
``@pytest.mark.parametrize`` line. Not every property's propagation tests fit
this shape (a relation-scoped walk over several sources is not one row of
facts), so this is applied where it fits rather than forced everywhere.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K")


@dataclass(frozen=True, slots=True)
class PropagationCase(Generic[K]):
    """One row: ``sql`` propagated over facts declared on named source columns,
    checked at the named output column against ``expected``. ``facts`` is required
    (not defaulted to empty) so a property with nothing to declare passes ``{}``
    explicitly at the call site, visible in the row rather than implied."""

    id: str
    sql: str
    out: str
    expected: K
    facts: Mapping[str, K]


def run_propagation_case(
    case: PropagationCase[K], run: Callable[[str, Mapping[str, K]], Mapping[str, K]]
) -> None:
    """Run ``case`` through ``run`` (sql, facts) -> {output column: value} and
    assert its output matches. ``run`` is the one piece of property-specific
    plumbing a test file supplies (how it builds its graph and property)."""
    actual = run(case.sql, case.facts)
    assert case.out in actual, f"{case.id}: {case.out!r} was not among the propagated outputs"
    assert actual[case.out] == case.expected, (
        f"{case.id}: expected {case.expected!r} at {case.out!r}, got {actual[case.out]!r}"
    )
