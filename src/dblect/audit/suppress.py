"""Parse and apply SQLFluff-compatible ``-- noqa`` suppression comments in model SQL.

The user can mute a detector finding by placing a SQL comment either on the line
containing the offending expression or on the line immediately above it. The syntax
is the one SQLFluff (and dbt Fusion's ``dbt lint``) already speak, so a single
comment can address both a lint rule and a dblect finding::

    -- noqa
    select b.k, sum(amount) from a left join b on a.k = b.k group by b.k

    -- only silence one dblect detector:
    select b.k, sum(amount) from a left join b on a.k = b.k group by b.k  -- noqa: DBLECT_NULL_GROUP_AFTER_OUTER_JOIN

    -- one directive, two audiences (the lint rule is dbt lint's, the DBLECT_ code is ours):
    select b.k, sum(amount) from a left join b on a.k = b.k group by b.k  -- noqa: RF01, DBLECT_JOIN_FANOUT

Two rules govern the codes after the colon:

* A bare ``-- noqa`` (no codes) silences every dblect finding on the line.
* ``-- noqa: <codes>`` silences only the dblect findings whose code is named.
  A dblect code is ``DBLECT_`` plus the finding kind's value uppercased
  (:func:`dblect.sql.suppression_code`). Codes that do not start with ``DBLECT_``
  are real lint rule codes (``RF01`` and friends): dbt lint owns them, so we ignore
  them. A directive naming only foreign codes silences nothing of ours.

There is no reason slot: SQLFluff noqa has none, and dropping it keeps our directives
interchangeable with the linter's. Every suppression is still logged in the report's
suppressed section, so a silenced finding is never invisible in review.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Protocol, TypeAlias, TypeVar

from dblect.sql import FindingKind, suppression_code

if TYPE_CHECKING:
    from dblect.check.findings import CheckFindingKind

    # A directive can name a kind from either detector family: a structural
    # ``FindingKind`` or a declaration-level ``CheckFindingKind``. The same
    # ``-- noqa`` syntax and scanner serve both, so one comment style acknowledges
    # any finding the analysis emits. ``CheckFindingKind`` is referenced only under
    # ``TYPE_CHECKING`` so this audit-side scanner does not pull the declaration-check
    # package in at import time, which would close a cycle.
    SuppressibleKind: TypeAlias = FindingKind | CheckFindingKind


class Suppressible(Protocol):
    """What a directive needs to decide whether it silences a finding: the kind it
    carries and the line span it occupies. Both families satisfy this, so matching is
    written once over the protocol rather than per family."""

    @property
    def kind(self) -> SuppressibleKind: ...
    @property
    def line_start(self) -> int: ...
    @property
    def line_end(self) -> int: ...


_F = TypeVar("_F", bound=Suppressible)

# Match ``noqa`` only when it stands alone: followed by ``:``, whitespace, or
# end-of-token. The ``(?![\w-])`` is load-bearing: it stops ``noqa-file`` and
# ``noqa-fixture`` (SQLFluff's file-level directive, and dblect's retired one) from
# being misread as a bare ``noqa`` that silences everything. That misread is the
# exact collision this rewrite exists to avoid.
_NOQA = re.compile(r"--\s*noqa(?![\w-])\s*(?::\s*(?P<codes>[^\n]*))?", re.IGNORECASE)


@cache
def _kind_by_code() -> dict[str, SuppressibleKind]:
    """The kind a ``DBLECT_`` code names, across both families. Built on first use
    rather than at import so the structural-audit module and the declaration-check
    module can both reach this scanner without a load-order cycle; the families' codes
    are disjoint, so the two maps merge without collision."""
    from dblect.check.findings import CheckFindingKind

    merged: dict[str, SuppressibleKind] = {suppression_code(k): k for k in FindingKind}
    merged.update({suppression_code(k): k for k in CheckFindingKind})
    return merged


@dataclass(frozen=True, slots=True)
class SuppressionDirective:
    """One ``-- noqa`` comment parsed out of model SQL.

    ``kinds`` is ``None`` for a bare ``-- noqa`` (silences every kind on the line).
    Otherwise it is the set of dblect kinds the directive's codes mapped to, which may
    be empty when every code was foreign (a ``-- noqa: RF01`` that names no dblect
    code silences nothing of ours)."""

    line: int
    kinds: frozenset[SuppressibleKind] | None


def parse_directives(sql: str) -> tuple[SuppressionDirective, ...]:
    """Pull every ``-- noqa`` directive out of `sql`. Lines are 1-indexed.

    A line with no codes after ``noqa`` (or only whitespace) yields a bare directive
    (``kinds is None``). A line with codes splits them on ``,``, uppercases each, maps
    it through the ``DBLECT_`` code table, and keeps the kinds that resolved; foreign
    codes are dropped, so the resulting ``kinds`` frozenset may be empty.
    """
    directives: list[SuppressionDirective] = []
    for line_idx, line_text in enumerate(sql.splitlines(), start=1):
        m = _NOQA.search(line_text)
        if m is None:
            continue
        raw_codes = (m.group("codes") or "").strip()
        if not raw_codes:
            directives.append(SuppressionDirective(line=line_idx, kinds=None))
            continue
        table = _kind_by_code()
        mapped = (table.get(code.strip().upper()) for code in raw_codes.split(","))
        kinds: frozenset[SuppressibleKind] = frozenset(k for k in mapped if k is not None)
        directives.append(SuppressionDirective(line=line_idx, kinds=kinds))
    return tuple(directives)


def directive_matches(directive: SuppressionDirective, finding: Suppressible) -> bool:
    """True if `directive` silences `finding`.

    A directive applies when it sits on the line immediately above the finding's span
    or anywhere within the span itself. A bare directive (``kinds is None``) silences
    every kind; a coded directive silences only the kinds it names (and a kind from one
    family never matches a finding from the other, since the codes are distinct).
    Findings without a line range (``line_start == 0``) are never suppressed: a
    directive can't responsibly silence what it can't locate.
    """
    if finding.line_start == 0:
        return False
    if not (finding.line_start - 1 <= directive.line <= finding.line_end):
        return False
    if directive.kinds is None:
        return True
    return finding.kind in directive.kinds


def apply(
    findings: Iterable[_F],
    directives: Iterable[SuppressionDirective],
) -> tuple[tuple[_F, ...], tuple[tuple[_F, SuppressionDirective], ...]]:
    """Partition `findings` into (active, suppressed-with-directive). Generic over the
    finding family: it works the same for a structural ``Finding`` and a
    declaration-level ``CheckFinding``, since both carry the kind and line span the
    match reads."""
    directives = tuple(directives)
    active: list[_F] = []
    suppressed: list[tuple[_F, SuppressionDirective]] = []
    for f in findings:
        match = next((d for d in directives if directive_matches(d, f)), None)
        if match is None:
            active.append(f)
        else:
            suppressed.append((f, match))
    return tuple(active), tuple(suppressed)
