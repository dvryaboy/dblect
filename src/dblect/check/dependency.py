"""Checking a declared dependency against the SQL that is supposed to keep it.

The dependency twin of the grain check: a declared ``zip determines city`` is
judged against what the model's SQL derives on its own, before the declaration is
folded in. SQL that re-derives it (a GROUP BY on zip, a source that carries it)
establishes it, and failing to re-derive it is not evidence by itself, since the
walk prefers silence to guessing. What counts as evidence is a UNION whose every
arm carries the dependency, because two arms may map one zip to different cities:
the merge is where the guarantee stops. A union with an arm the walk knows nothing
about is no evidence, since that arm may have lost the dependency earlier. Even then
the data may agree on every shared zip, so the finding says not established rather
than violated, and warns rather than errors, the grade
``docs/design/refutation-and-verdicts.md`` assigns the grain finding too.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from dblect.audit.sourcemap import LineMap
from dblect.check.declared import judged_declarations
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.check.locate import file_of, source_span, span_of
from dblect.lineage.facts.model import Annotation, Fact
from dblect.lineage.graph import RelationLineageGraph, SourceRef
from dblect.lineage.properties.functional_dependency import (
    FD,
    FDSet,
    UnionMerge,
    determines,
    union_merges,
)
from dblect.manifest import Manifest
from dblect.sql import _sqlglot as sg


def dependency_witness(declared: FD, merges: Iterable[UnionMerge]) -> UnionMerge | None:
    """The union a declared dependency is dropped at, if there is one: every arm
    entails it, so an arm carrying ``zip -> region`` and ``region -> city`` counts.
    Ties go to the earliest in the SQL, so the finding reads the same every run."""
    witnesses = [
        merge
        for merge in merges
        if all(
            determines(FDSet(arm), declared.determinant, declared.dependent) for arm in merge.arms
        )
    ]
    if not witnesses:
        return None
    return min(witnesses, key=lambda merge: sg.line_range(merge.union) or (0, 0))


def declared_dependency_findings(
    manifest: Manifest,
    fd_facts: Mapping[SourceRef, tuple[Fact[FDSet, SourceRef], ...]],
    inferred: Mapping[SourceRef, Annotation[FDSet]],
    flow: Mapping[SourceRef, Annotation[FDSet]],
    relations: RelationLineageGraph,
    line_maps: dict[str, LineMap],
) -> list[CheckFinding]:
    """One finding per declared dependency this model's SQL drops at a union.

    Declarations are judged against ``inferred``, what each model's SQL derived on
    its own. The union walk reads this model's sources at ``flow`` instead, which
    folds in their declarations: upstream claims are trusted, and the only claim
    under test is this model's own."""
    model_fds = {ref: ann.value for ref, ann in flow.items()}
    out: list[CheckFinding] = []
    judged: set[tuple[SourceRef, FD]] = set()
    merges_of: dict[SourceRef, frozenset[UnionMerge]] = {}
    for scope, fact, derived in judged_declarations(fd_facts, inferred):
        for declared in fact.value.fds:
            if (scope, declared) in judged:
                continue
            judged.add((scope, declared))
            if determines(derived, declared.determinant, declared.dependent):
                continue
            if scope not in merges_of:
                tree = relations.derivation(scope)
                merges_of[scope] = (
                    union_merges(tree, model_fds) if tree is not None else frozenset()
                )
            witness = dependency_witness(declared, merges_of[scope])
            if witness is None:
                continue
            out.append(_finding(manifest, scope, fact, declared, witness, line_maps))
    return out


def _finding(
    manifest: Manifest,
    scope: SourceRef,
    fact: Fact[FDSet, SourceRef],
    declared: FD,
    witness: UnionMerge,
    line_maps: dict[str, LineMap],
) -> CheckFinding:
    determinant = ", ".join(sorted(declared.determinant))
    attribution = f" (declared by {fact.detail})" if fact.detail else ""
    line_start, line_end = span_of(witness.union)
    return CheckFinding(
        kind=CheckFindingKind.DEPENDENCY_NOT_ESTABLISHED,
        message=(
            f"declared dependency ({determinant} -> {declared.dependent}){attribution} is "
            "not established: every arm of the UNION carries it, but the merge does "
            f"not, since two arms may map one value of ({determinant}) to different "
            f"values of {declared.dependent}. Settle the mapping from one source "
            "before the union, or correct the declaration to what the SQL produces."
        ),
        model_unique_id=scope.unique_id,
        file_path=file_of(manifest, scope),
        column=declared.dependent,
        line_start=line_start,
        line_end=line_end,
        source_span=source_span(manifest, scope.unique_id, line_start, line_end, line_maps),
    )
