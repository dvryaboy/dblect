"""Parse and apply ``-- noqa-fixture:`` suppression comments in model SQL.

The user can mute an individual detector finding by placing a SQL comment
either on the line containing the offending expression or on the line
immediately above it::

    -- noqa-fixture: orphan handling lives in the downstream contract
    select b.k, sum(amount) from a left join b on a.k = b.k group by b.k

    -- only silence one kind:
    select b.k, sum(amount) from a left join b on a.k = b.k group by b.k  -- noqa-fixture: null_group_after_outer_join: orphan handling

Two rules govern the body of the comment:

* If the body starts with ``<finding_kind>:`` where ``finding_kind`` is a
  known ``FindingKind`` value, the directive is kind-specific and silences
  only that detector. Otherwise the directive silences every kind on the
  line, and the whole body is the reason. This avoids treating a free-text
  reason like ``TODO: revisit Q3`` as a kind claim.
* A reason is required. A bare ``-- noqa-fixture`` (or ``-- noqa-fixture:``
  with only whitespace after) does not silence anything; instead it surfaces
  as a ``MALFORMED_SUPPRESSION`` finding so the dangling directive is visible
  in review.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from dblect.sql import Finding, FindingKind

_NOQA = re.compile(r"--\s*noqa-fixture\b\s*(?::\s*(?P<body>.*))?", re.IGNORECASE)
_KIND_CLAIM = re.compile(r"^(?P<kind>[a-z_]+)\s*:\s*(?P<reason>.+)$")
_FINDING_KIND_VALUES: frozenset[str] = frozenset(k.value for k in FindingKind)


@dataclass(frozen=True, slots=True)
class SuppressionDirective:
    """One ``-- noqa-fixture:`` comment parsed out of model SQL."""

    line: int
    kind: FindingKind | None
    reason: str


def parse_directives(sql: str) -> tuple[tuple[SuppressionDirective, ...], tuple[Finding, ...]]:
    """Pull every well-formed directive out of `sql`, plus malformed-comment findings.

    Lines are 1-indexed. The second tuple is `MALFORMED_SUPPRESSION` findings
    for bare or empty ``-- noqa-fixture`` comments; they ride the regular
    finding pipeline so the user sees them in the report.
    """
    directives: list[SuppressionDirective] = []
    malformed: list[Finding] = []
    for line_idx, line_text in enumerate(sql.splitlines(), start=1):
        m = _NOQA.search(line_text)
        if m is None:
            continue
        body = (m.group("body") or "").strip()
        if not body:
            malformed.append(
                Finding(
                    kind=FindingKind.MALFORMED_SUPPRESSION,
                    message="noqa-fixture comment requires a reason",
                    sql_snippet=line_text.strip(),
                    line_start=line_idx,
                    line_end=line_idx,
                )
            )
            continue
        kind, reason = _split_kind_claim(body)
        directives.append(SuppressionDirective(line=line_idx, kind=kind, reason=reason))
    return tuple(directives), tuple(malformed)


def _split_kind_claim(body: str) -> tuple[FindingKind | None, str]:
    """Read a kind-specific claim from `body`, else return (None, body)."""
    m = _KIND_CLAIM.match(body)
    if m is None:
        return None, body
    claimed = m.group("kind").lower()
    if claimed not in _FINDING_KIND_VALUES:
        # Looks like a kind claim but isn't a known kind; treat as plain reason
        # so a typo like `unordered_window: …` doesn't silently fail to suppress.
        # The "all kinds" fallback is safer than a silent miss.
        return None, body
    return FindingKind(claimed), m.group("reason").strip()


def directive_matches(directive: SuppressionDirective, finding: Finding) -> bool:
    """True if `directive` silences `finding`.

    A directive applies when it sits on the line immediately above the
    finding's span or anywhere within the span itself. A directive without a
    `kind` silences every kind; a kind-specific directive only silences its
    own kind. Findings without a line range (``line_start == 0``) are never
    suppressed: a directive can't responsibly silence what it can't locate.
    """
    if finding.line_start == 0:
        return False
    if directive.kind is not None and directive.kind is not finding.kind:
        return False
    return finding.line_start - 1 <= directive.line <= finding.line_end


def apply(
    findings: Iterable[Finding],
    directives: Iterable[SuppressionDirective],
) -> tuple[tuple[Finding, ...], tuple[tuple[Finding, SuppressionDirective], ...]]:
    """Partition `findings` into (active, suppressed-with-directive)."""
    directives = tuple(directives)
    active: list[Finding] = []
    suppressed: list[tuple[Finding, SuppressionDirective]] = []
    for f in findings:
        match = next((d for d in directives if directive_matches(d, f)), None)
        if match is None:
            active.append(f)
        else:
            suppressed.append((f, match))
    return tuple(active), tuple(suppressed)
