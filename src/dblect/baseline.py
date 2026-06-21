"""Report the findings a change introduces, by diffing against a base manifest.

``dblect check --base-manifest`` analyses the base revision's manifest the same way
as HEAD and keeps the findings whose identity is new. The diff is over finding sets,
not edited source lines: a finding is computed over compiled SQL grounded against
upstream models, so a changed macro or an upstream column can introduce one with the
model's own source file unchanged, which a source-line diff would not see.
"""

from __future__ import annotations

from collections.abc import Iterable

from dblect.analysis import AnalysisFinding, cross_world_identity


def introduced_findings(
    head: Iterable[AnalysisFinding], base: Iterable[AnalysisFinding]
) -> tuple[AnalysisFinding, ...]:
    """The ``head`` findings whose cross-world identity is absent from ``base``.

    Pure over its inputs, so the contract is exercised without compiling a manifest.
    A finding that merely moved lines or changed wording between the two worlds keeps
    its identity, so it reads as preexisting and drops; only a genuinely new
    ``(kind, model, subject)`` survives. A finding that exists in the base but not in
    HEAD (one the change *fixed*) is simply not in ``head``, so it never appears here.
    """
    base_identities = {cross_world_identity(f) for f in base}
    return tuple(f for f in head if cross_world_identity(f) not in base_identities)
