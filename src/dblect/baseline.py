"""Limit findings to the ones a change introduces, by diffing against a base manifest.

On a pull request the findings worth a reviewer's attention are the ones the change
*introduces*, not the ones the project already carried. ``dblect check
--base-manifest <path>`` analyses the base revision's manifest the same way it
analyses HEAD, then reports only the findings whose stable identity is absent from
the base.

Why a finding-set diff rather than a source-line diff
-----------------------------------------------------

A structural finding is computed over a model's *compiled* SQL, which dbt renders
with refs and macros expanded inline, and grounded against semantics propagated from
upstream models (nullability, uniqueness, the fact substrate). So a finding can be
introduced with no edit to the model's own source file: a changed macro the model
calls, or an upstream column that turned nullable, leaves the model's file byte for
byte identical while a new hazard appears in its compiled SQL. Scoping a report by
edited source line cannot see those, because the file the diff touched is not the
model the finding lands on. Diffing the finding *sets* of two compilations sees them,
because it compares results rather than text. This is the differential mode mature
whole-program analysers use (Infer's ``reportdiff``, the stored-baseline suppression
of the type checkers) rather than the per-line scoping that suits a local linter.

The identity that survives the diff is :func:`~dblect.analysis.cross_world_identity`:
a finding's kind, the model it lands on, and what it is about (the offending snippet
for a structural finding; the column and contract for a declaration one), with the
message and line span that drift between compilations deliberately dropped. A finding
present in both worlds under that identity is preexisting and filtered; one present
only in HEAD is introduced and kept. The HEAD finding carries its own line numbers
for display, always accurate to HEAD, with no compiled-to-source mapping to get
wrong.
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
