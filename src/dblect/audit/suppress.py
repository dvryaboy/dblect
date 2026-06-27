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

Directives are read from both of a model's texts (:class:`FramedDirectives`): the source
frame from ``raw_code`` and the compiled frame from the rendered SQL. A finding is matched
in each frame against the coordinate that indexes it. Its back-mapped ``located_span`` is
matched against the source frame when the back-map placed it on a real source line, and
its ``compiled_span`` is matched against the compiled frame. A macro-emitted construct
occupies both: the ``{{ ... }}`` call line in the template and the emitted line in the
compiled SQL. So a ``-- noqa`` on the call line silences it from the source frame, and one
written in the macro body, which renders next to the construct in every expansion, silences
it from the compiled frame. That compiled-frame path lets a single comment in a shared
macro speak for every model the macro expands into, the case a source-only scan leaves
unsuppressable. Matching each frame against its own coordinate keeps a source directive
from silencing a compiled-relative finding by line-number coincidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Protocol, TypeAlias, TypeVar

from dblect.audit.sourcemap import SourceSpan, SpanBasis
from dblect.sql import FindingKind, suppression_code

if TYPE_CHECKING:
    from dblect.check.findings import CheckFindingKind
    from dblect.manifest import Node

    # A directive can name a kind from either detector family: a structural
    # ``FindingKind`` or a declaration-level ``CheckFindingKind``. The same
    # ``-- noqa`` syntax and scanner serve both, so one comment style acknowledges
    # any finding the analysis emits. ``CheckFindingKind`` is referenced only under
    # ``TYPE_CHECKING`` so this audit-side scanner does not pull the declaration-check
    # package in at import time, which would close a cycle.
    SuppressibleKind: TypeAlias = FindingKind | CheckFindingKind


class Suppressible(Protocol):
    """What a directive needs to decide whether it silences a finding: the kind it carries
    and the two coordinates it can occupy. Both finding families satisfy this, so matching
    is written once over the protocol rather than per family.

    ``located_span`` is the back-mapped span the report shows; ``compiled_span`` is the raw
    compiled coordinate the parser observed. They differ only for a macro-emitted construct,
    where ``located_span`` names the ``{{ ... }}`` call site in the template and
    ``compiled_span`` names the emitted line in the compiled SQL. Matching consults each
    frame against the coordinate that indexes it (:func:`apply`), so a ``-- noqa`` on the
    call line and one in the macro body both reach the finding, each in its own text."""

    @property
    def kind(self) -> SuppressibleKind: ...
    @property
    def located_span(self) -> SourceSpan: ...
    @property
    def compiled_span(self) -> SourceSpan: ...


_F = TypeVar("_F", bound=Suppressible)

# Match ``noqa`` only when it stands alone: followed by ``:``, whitespace, or
# end-of-token. The ``(?![\w-])`` is load-bearing: it stops ``noqa-file`` and
# ``noqa-fixture`` (SQLFluff's file-level directive, and dblect's retired one) from
# being misread as a bare ``noqa`` that silences everything. That misread is the
# exact collision this rewrite exists to avoid.
_NOQA = re.compile(r"--\s*noqa(?![\w-])\s*(?::\s*(?P<codes>[^\n]*))?", re.IGNORECASE)


def _comment_of(line: str) -> str | None:
    """The text from `line`'s first ``--`` comment marker, or ``None`` when the line
    has no comment outside a string literal.

    A ``--`` inside a ``'...'`` or ``"..."`` literal is data, not a comment, so it must
    not start the directive scan (a projection like ``select '-- noqa' as label`` would
    otherwise read as a bare suppression). Doubled quotes (SQL's in-literal escape)
    toggle the state twice, leaving it correct.
    """
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "-" and line[i + 1 : i + 2] == "-":
            return line[i:]
    return None


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
        comment = _comment_of(line_text)
        if comment is None:
            continue
        m = _NOQA.search(comment)
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


def _admits(directive: SuppressionDirective, span: SourceSpan, kind: SuppressibleKind) -> bool:
    """True if `directive`, read in `span`'s coordinate frame, silences a `kind` finding
    occupying `span`.

    A directive applies when it sits on the line immediately above the span or anywhere
    within it. A bare directive (``kinds is None``) silences every kind; a coded directive
    silences only the kinds it names (and a kind from one family never matches a finding
    from the other, since the codes are distinct). A span with no line range
    (``line_start == 0``) is never matched: a directive can't responsibly silence what it
    can't locate.
    """
    if span.line_start == 0:
        return False
    if not (span.line_start - 1 <= directive.line <= span.line_end):
        return False
    if directive.kinds is None:
        return True
    return kind in directive.kinds


def directive_matches(directive: SuppressionDirective, finding: Suppressible) -> bool:
    """True if `directive` silences `finding` on its back-mapped ``located_span``, the
    coordinate the report shows. The full two-frame match lives in :func:`apply`."""
    return _admits(directive, finding.located_span, finding.kind)


@dataclass(frozen=True, slots=True)
class FramedDirectives:
    """The ``-- noqa`` directives a model offers, split by the text they were read from.

    ``source`` directives come from ``raw_code`` (the developer's template); ``compiled``
    directives come from the rendered SQL. :func:`apply` matches a finding against each
    frame using the coordinate that indexes it, so the two never cross."""

    source: tuple[SuppressionDirective, ...]
    compiled: tuple[SuppressionDirective, ...]

    @classmethod
    def parse(cls, *, raw: str | None, compiled: str | None) -> FramedDirectives:
        """Parse both frames from a model's two texts. Either text absent yields an empty
        frame, so a model with no template (or no compiled SQL) simply offers fewer
        directives rather than failing."""
        return cls(parse_directives(raw or ""), parse_directives(compiled or ""))

    @classmethod
    def for_node(cls, node: Node) -> FramedDirectives:
        """Both frames for a manifest node: the source frame from its template and the
        compiled frame from the SQL the analysis layer parses. The single place the
        node-field-to-frame binding lives, shared by the structural and declaration runs."""
        return cls.parse(raw=node.raw_code, compiled=node.analysis_sql)


def format_directive_location(*, in_compiled: bool, line: int) -> str:
    """Render where a directive sat for a human-facing surface. A compiled-frame match (a
    macro body's ``-- noqa``) is tagged ``compiled`` so its line is read in compiled space
    rather than mistaken for a source line. Shared by the text report and the SARIF log so
    the two label a suppression the same way."""
    return f"compiled L{line}" if in_compiled else f"L{line}"


def apply(
    findings: Iterable[_F],
    directives: FramedDirectives,
) -> tuple[tuple[_F, ...], tuple[tuple[_F, SuppressionDirective, bool], ...]]:
    """Partition `findings` into (active, suppressed). Each suppressed entry is the finding,
    the directive that silenced it, and whether that directive was read in the compiled
    frame. Generic over the finding family: it works the same for a structural ``Finding``
    and a declaration-level ``CheckFinding``, since both expose the protocol the match reads.

    A finding is matched in each frame against the coordinate that indexes it: the source
    frame against its ``located_span`` when the back-map anchored it to a real source line
    (``SOURCE`` or ``MACRO_CALL``), and the compiled frame against its ``compiled_span``
    whenever that span is a genuine compiled coordinate (``MACRO_CALL`` or ``COMPILED``). A
    macro-emitted construct occupies both, so a call-line ``-- noqa`` and a macro-body one
    each reach it. The source frame is preferred when both match, since it names the line the
    developer wrote. A purely source-anchored finding is never matched against the compiled
    frame (its compiled coordinate is the same text), and a compiled-relative one is never
    matched against the source frame, so a source directive cannot silence it by coincidence.
    """
    active: list[_F] = []
    suppressed: list[tuple[_F, SuppressionDirective, bool]] = []
    for f in findings:
        match = _suppressing_directive(f, directives)
        if match is None:
            active.append(f)
        else:
            suppressed.append((f, match[0], match[1]))
    return tuple(active), tuple(suppressed)


def _suppressing_directive(
    finding: Suppressible, directives: FramedDirectives
) -> tuple[SuppressionDirective, bool] | None:
    """The directive that silences `finding`, paired with whether it came from the compiled
    frame, or ``None`` if none does. Source frame first (the line the developer wrote)."""
    located = finding.located_span
    if located.basis is not SpanBasis.COMPILED:
        source = next((d for d in directives.source if _admits(d, located, finding.kind)), None)
        if source is not None:
            return source, False
    if located.basis is not SpanBasis.SOURCE:
        compiled_span = finding.compiled_span
        compiled = next(
            (d for d in directives.compiled if _admits(d, compiled_span, finding.kind)), None
        )
        if compiled is not None:
            return compiled, True
    return None
